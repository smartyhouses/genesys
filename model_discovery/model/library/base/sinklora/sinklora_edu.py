# Modified based on https://github.com/dvlab-research/LongLoRA

from typing import Optional, Tuple
import warnings
import torch
import transformers

# from einops import rearrange
# from flash_attn import flash_attn_varlen_qkvpacked_func
# from flash_attn.bert_padding import unpad_input, pad_input


group_size_ratio = 1/4

def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)

def apply_rotary_pos_emb(q, k, cos, sin, position_ids):
    gather_indices = position_ids[:, None, :, None]  # [bs, 1, seq_len, 1]
    gather_indices = gather_indices.repeat(1, cos.shape[1], 1, cos.shape[3])
    cos = torch.gather(cos.repeat(gather_indices.shape[0], 1, 1, 1).to(q.dtype), 2, gather_indices)
    sin = torch.gather(sin.repeat(gather_indices.shape[0], 1, 1, 1).to(k.dtype), 2, gather_indices)
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    return q_embed, k_embed



def get_forward_function(use_flash_attn=True, use_full=False):

    def forward_attention(
        self,
        hidden_states: torch.FloatTensor,
        attention_mask: torch.FloatTensor,
        position_ids: torch.LongTensor,
        head_mask: Optional[torch.FloatTensor] = None,
        layer_past: Optional[Tuple[torch.Tensor]] = None,
        use_cache: Optional[bool] = False,
        output_attentions: Optional[bool] = False,
    ):
        # NOTE: compute SS group size
        bsz, q_len, _ = hidden_states.size()
        has_layer_past = layer_past is not None

        # Compute QKV
        # Attention heads [batch, seq_len, hidden_size]
        #   --> [batch, seq_len, (np * 3 * head_size)]
        qkv = self.query_key_value(hidden_states)

        # [batch, seq_len, (num_heads * 3 * head_size)]
        #   --> [batch, seq_len, num_heads, 3 * head_size]
        new_qkv_shape = qkv.size()[:-1] + (self.num_attention_heads, 3 * self.head_size)
        qkv = qkv.view(*new_qkv_shape)

        # [batch, seq_len, num_attention_heads, 3 * head_size]
        #   --> 3 [batch, num_attention_heads, seq_len, head_size]
        query = qkv[..., : self.head_size].permute(0, 2, 1, 3)
        key = qkv[..., self.head_size : 2 * self.head_size].permute(0, 2, 1, 3)
        value = qkv[..., 2 * self.head_size :].permute(0, 2, 1, 3)
        # [bsz, nh, q_len, hd]

        # Compute rotary embeddings on rotary_ndims
        query_rot = query[..., : self.rotary_ndims]
        query_pass = query[..., self.rotary_ndims :]
        key_rot = key[..., : self.rotary_ndims]
        key_pass = key[..., self.rotary_ndims :]

        # Compute token offset for rotary embeddings (when decoding)
        seq_len = key.shape[-2]
        if has_layer_past:
            seq_len += layer_past[0].shape[-2]
        cos, sin = self.rotary_emb(value, seq_len=seq_len)
        query, key = apply_rotary_pos_emb(query_rot, key_rot, cos, sin, position_ids)
        query = torch.cat((query, query_pass), dim=-1)
        key = torch.cat((key, key_pass), dim=-1)

        # Cache QKV values
        if has_layer_past:
            past_key = layer_past[0]
            past_value = layer_past[1]
            key = torch.cat((past_key, key), dim=-2)
            value = torch.cat((past_value, value), dim=-2)
        present = (key, value) if use_cache else None

        # NOTE: apply shift
        group_size = int(q_len * group_size_ratio)
        if q_len % group_size > 0:
            raise ValueError("q_len %d should be divisible by group size %d." % (q_len, group_size))
        num_group = q_len // group_size
        if self.training and not use_full:
            def shift(qkv, num_heads, head_dim):
                # qkv = [bsz, nh, q_len, d]
                qkv = qkv.transpose(1, 2)
                # qkv = [bsz, q_len, nh, d]
                qkv[:, :, num_heads//2:] = qkv[:, :, num_heads//2:].roll(-group_size//2, dims=1)

                # -> [bsz * n_group, group_s, nh, d)
                #   -> [bsz * n_group, nh, group_s, d)
                qkv = qkv.reshape(bsz * num_group, group_size, num_heads, head_dim).transpose(1, 2)
                return qkv

            # contiguous is required as self._attn() will attempt to apply .view() on them
            query = shift(query, self.num_attention_heads, self.head_size).contiguous()
            key = shift(key, self.num_attention_heads, self.head_size).contiguous()
            value = shift(value, self.num_attention_heads, self.head_size).contiguous()

            attention_mask = attention_mask[:, :, :group_size, :group_size].repeat(num_group, 1, 1, 1)

        # Compute attention
        if use_flash_attn:
            attn_output, attn_weights = _flash_attn(query, key, value, attention_mask, head_mask)
        else:
            attn_output, attn_weights = self._attn(query, key, value, attention_mask, head_mask)

        # NOTE: shift back
        if self.training and not use_full:
            attn_output = attn_output.transpose(1, 2).contiguous()
            attn_output = attn_output.reshape(bsz, q_len, self.num_attention_heads, self.head_size)
            # [bsz, q_len, nh, hd]
            attn_output[:, :, self.num_attention_heads//2:] = attn_output[:, :, self.num_attention_heads//2:].roll(group_size//2, dims=1)
            attn_output = attn_output.transpose(1, 2)

        # Reshape outputs
        attn_output = self._merge_heads(attn_output, self.num_attention_heads, self.head_size)
        attn_output = self.dense(attn_output)

        outputs = (attn_output, present)
        if output_attentions:
            outputs += (attn_weights,)

        return outputs

    return forward_attention


def replace_gpt_neox_attn(use_flash_attn=True, use_full=False):
    cuda_major, cuda_minor = torch.cuda.get_device_capability()
    if use_flash_attn and cuda_major < 8:
        warnings.warn(
            "Flash attention is only supported on A100 or H100 GPU during training due to head dim > 64 backward."
            "ref: https://github.com/HazyResearch/flash-attention/issues/190#issuecomment-1523359593"
            "Resorting to plain attention..."
        )
        use_flash_attn = False

    forward_fn = get_forward_function(use_flash_attn, use_full)
    transformers.models.gpt_neox.modeling_gpt_neox.GPTNeoXAttention.forward = forward_fn