import math
import torch
from torch import nn, einsum

from einops import rearrange
from einops.layers.torch import Rearrange, Reduce

from torch.cuda.amp import autocast

from collections import namedtuple
import torch.nn.functional as F


def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def log(t, eps = 1e-20):
    return torch.log(t.clamp(min = eps))

@autocast(enabled = False)
def coor_descent(
    s,
    *,
    n_iters,
    k,
    eps = 1e-1,
    eps_init = None,
    eps_decay = 1.,
    mask = None
):
    """
    coordinate descent  - https://arxiv.org/abs/1502.04759, utilized in https://arxiv.org/abs/2303.09752
    ε-scaling           - https://arxiv.org/abs/1610.06519, utilized in https://arxiv.org/abs/2304.04947

    in a follow up paper applying coordinate descent routing to efficient fine tuning
    they were able to cut n_iters from 50 -> 20 by setting eps_init = 4 and eps_decay = 0.7
    eps was dependent on the task, and ranged from 0.02 to 1
    """

    assert n_iters > 0

    mask_value = -torch.finfo(s.dtype).max

    if not isinstance(k, torch.Tensor):
        k = torch.Tensor([k]).to(s)
    else:
        k = rearrange(k, '... -> ... 1')

    logk = log(k)

    if exists(mask):
        s = s.masked_fill(~mask, mask_value)

    a = 0
    b = -s

    current_eps = max(default(eps_init, eps), eps)

    for _ in range(n_iters):
        sb = ((s + b) / current_eps)

        if exists(mask):
            sb = sb.masked_fill(~mask, mask_value)

        a = current_eps * (logk - sb.logsumexp(dim = -1, keepdim = True))
        b = -F.relu(s + a)

        current_eps = max(current_eps * eps_decay, eps)

    scores = ((s + a + b) / current_eps).exp()

    if exists(mask):
        scores = scores.masked_fill(~mask, 0.)

    return scores

TopkReturn = namedtuple('TopkReturn', ['values', 'indices', 'coor_descent_values', 'gates'])

@autocast(enabled = False)
def topk(
    x,
    k,
    coor_descent_k_ratio = 9 / 8,
    n_iters = 20,
    eps = 1e-1,
    eps_init = None,
    eps_decay = 1.,
    mask = None,
    fused = False,
    non_differentiable = False
):
    """
    differentiable top-k on last dimension
    """

    if non_differentiable:
        values, indices = torch.topk(x, k = k, dim = -1)
        return TopkReturn(values, indices, None, None)

    assert coor_descent_k_ratio >= 1.
    assert k > 0

    # whether to used fused kernel or not

    fn = coor_descent

    if fused and x.is_cuda:
        from colt5_attention.triton_coor_descent import triton_coor_descent
        fn = triton_coor_descent

    # do coordinate descent for gradients

    coor_descent_out = fn(
        x,
        k = min(k * coor_descent_k_ratio, x.shape[-1]),   # fetch a bit more for better learning, as in CoLT5 paper (they fetched 9 / 8 times more)
        mask = mask,
        n_iters = n_iters,
        eps = eps,
        eps_init = eps_init,
        eps_decay = eps_decay
    )

    # do straight through

    gates = coor_descent_out + (1 - coor_descent_out).detach()

    x = x * gates

    # hard topk

    values, indices = torch.topk(x, k, dim = -1)

    # return something that looks like a usual topk, but now differentiable

    coor_descent_values = coor_descent_out.gather(-1, indices)
    gates = gates.gather(-1, indices)

    return TopkReturn(values, indices, coor_descent_values, gates)


# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def log(t, eps = 1e-20):
    return torch.log(t.clamp(min = eps))

def gumbel_noise(t):
    noise = torch.zeros_like(t).uniform_(0, 1)
    return -log(-log(noise))

# init

def init_(t, dim = None):
    dim = default(dim, t.shape[-1])
    std = 1. / math.sqrt(dim)
    return nn.init.normal_(t, mean=0, std=std)

# optimizer

def list_subtract(l, r):
    return [el for el in l if el not in set(r)]

def fetch_pkm_value_parameters(module):
    params = []
    for m in module.modules():
        if isinstance(m, PKM):
            params.append(m.values.weight)
    rest = list_subtract(module.parameters(), params)
    return params, rest

def fetch_optimizer_parameters(module, pkm_learning_rate = 1e-2):
    pkm_params, rest = fetch_pkm_value_parameters(module)
    return [{'params': rest}, {'params': pkm_params, 'lr': pkm_learning_rate}]

# norm

class MaskedBatchNorm1D(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(
        self,
        x,
        mask = None
    ):
        if exists(mask):
            initial_x = x
            x = x[mask]

        x = self.fn(x)

        if exists(mask):
            initial_x[mask] = x
            x = initial_x

        return x

class PKM(nn.Module):
    def __init__(
        self,
        dim,
        heads = 4,
        num_keys = 128,
        topk = 32,
        dim_head = 128,
        input_dropout = 0.,
        query_dropout = 0.,
        value_dropout = 0.,
        attn_dropout = 0.,
        use_layernorm = True,
        pre_layernorm = False,
        differentiable_topk = False,
        concat_values_and_combine = False,
        norm_output = False
    ):
        super().__init__()
        self.topk = topk
        self.heads = heads
        self.num_keys = num_keys

        dim_query = dim_head * heads * 2
        self.to_queries = nn.Linear(dim, dim_query, bias = False)

        # pre-layernorm pattern

        self.pre_layernorm = nn.LayerNorm(dim) if pre_layernorm else nn.Identity()

        # batchnorm would break causality

        self.use_layernorm = use_layernorm

        if use_layernorm:
            self.norm = nn.LayerNorm(dim_head)
        else:
            self.norm = MaskedBatchNorm1D(nn.BatchNorm1d(dim_head))

        # keys

        self.keys = nn.Parameter(torch.zeros(heads, num_keys, 2, dim_head))
        init_(self.keys)

        # values

        self.concat_values_and_combine = concat_values_and_combine

        if concat_values_and_combine:
            values = nn.Embedding(num_keys ** 2, dim_head)

            self.values = nn.Sequential(
                values,
                Reduce('b (h k) d -> b h d', 'sum', h = heads),
                Rearrange('b n d -> b (n d)'),
                nn.Linear(dim_head * heads, dim, bias = False)
            )
        else:
            values = nn.EmbeddingBag(num_keys ** 2, dim, mode = 'sum')
            self.values = values


        init_(values.weight)

        # dropouts

        self.input_dropout = nn.Dropout(input_dropout)
        self.query_dropout = nn.Dropout(query_dropout)
        self.value_dropout = nn.Dropout(value_dropout)
        self.attn_dropout = nn.Dropout(attn_dropout)

        # use a differentiable topk, based on coordinate descent

        self.differentiable_topk = differentiable_topk

        # https://arxiv.org/abs/2302.06461
        # claims to boost performance of softmax key / value networks by simply layernorming the output

        self.output_norm = nn.LayerNorm(dim) if norm_output else nn.Identity()

    def forward(
        self,
        x,
        input_mask = None,
        gumbel_noise_scale = 0.,
        **kwargs
    ):
        b, t, h = *x.shape[:2], self.heads

        x = self.pre_layernorm(x)
        x = self.input_dropout(x)

        queries = self.to_queries(x)

        # split out query heads

        queries = rearrange(queries, 'b t (p h d) -> (b p h) t d', p = 2, h = h)

        # norm and dropout queries

        norm_kwargs = dict(mask = input_mask) if not self.use_layernorm else dict()
        queries = self.norm(queries, **norm_kwargs)
        queries = self.query_dropout(queries)

        # ready queries

        queries = rearrange(queries, '(b p h) t d -> p b t h d', p = 2, h = h)

        # similarity to keys

        dots = einsum('p b t h d, h n p d -> b t h p n', queries, self.keys)

        # gumbel noise

        if gumbel_noise_scale > 0.:
            dots = dots + gumbel_noise(dots) * gumbel_noise_scale

        # topk scores

        if self.differentiable_topk:
            scores, indices, *_ = topk(dots, k = self.topk, fused = True)
        else:
            scores, indices = dots.topk(k = self.topk, dim = -1)

        # scores are factorized

        (scores_x, scores_y), (indices_x, indices_y) = map(lambda t: t.chunk(2, dim = 3), (scores, indices))

        all_topk = self.topk ** 2

        all_scores = rearrange((
            rearrange(scores_x, '... k -> ... k 1') +
            rearrange(scores_y, '... k -> ... 1 k')
        ), 'b t h ... -> b t h (...)')

        all_indices = rearrange((
            rearrange(indices_x, '... k -> ... k 1') * self.num_keys +
            rearrange(indices_y, '... k -> ... 1 k')
        ), 'b t h ... -> b t h (...)')

        final_topk, final_indices = all_scores.topk(self.topk, dim=-1)
        value_indices = all_indices.gather(-1, final_indices)

        # attention

        attn = final_topk.softmax(dim=-1)
        attn = self.attn_dropout(attn)

        value_indices, attn = map(lambda t: rearrange(t, 'b t h k -> (b t) (h k)'), (value_indices, attn))

        # aggregate

        if self.concat_values_and_combine:
            out = self.values(value_indices)
        else:
            out = self.values(value_indices, per_sample_weights = attn)

        out = self.value_dropout(out)

        # maybe layernorm the output

        out = self.output_norm(out)

        return rearrange(out, '(b t) d -> b t d', b = b)