''' Define the sublayers in encoder/decoder layer '''
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from synthesizer.modules import ScaledDotProductAttention, DenseAttention

__author__ = "Tenzin Singhay Bhotia, Yu-Hsiang Huang"


class RandomAttention(nn.Module):
    def __init__(self, batch_size, n_head, max_seq_len, attn_dropout = 0.1):
        super(RandomAttention, self).__init__()
        #device = torch.device("GPU"),
        self.random_attn = torch.randn(batch_size, n_head, max_seq_len, max_seq_len, requires_grad = True)
        self.dropout = nn.Dropout(attn_dropout)

    def forward(self, v, len_q, mask=None):

        # b x n x max_len x max_len -> b x n x lq x lq
        random_attn = self.random_attn[:mask.shape[0],:,:len_q,:len_q]
        random_attn = random_attn.to(torch.device('cuda' if mask.is_cuda else 'cpu'))

        if mask is not None:
            random_attn = random_attn.masked_fill(mask == 0, -1e9)

        random_attn = self.dropout(F.softmax(random_attn, dim=-1))
        output = torch.matmul(random_attn, v)
        
        return output, random_attn

class FactorizedRandomAttention(nn.Module):
    def __init__(self, batch_size, n_head, f,  max_seq_len, attn_dropout = 0.1):
        super(RandomAttention, self).__init__()
        #device = torch.device("GPU"),
        self.random_attn_1 = torch.randn(batch_size, n_head, max_seq_len, f, requires_grad = True)
        self.random_attn_2 = torch.randn(batch_size, n_head, f, max_seq_len, requires_grad = True)
        self.dropout = nn.Dropout(attn_dropout)
    def forward(self, v, len_q, mask=None, factorize=False):
        # b x n x max_len x max_len -> b x n x lq x lq #[:,:,:len_q,:len_q]
        random_attn = torch.matmul(self.random_attn_1, self.random_attn_2)[:mask.shape[0],:,:len_q,:len_q]

        if mask is not None:
            random_attn = random_attn.masked_fill(mask == 0, -1e9)

        random_attn = self.dropout(F.softmax(random_attn, dim=-1))
        output = torch.matmul(random_attn, v)
        
        return output, random_attn

class MultiHeadAttention(nn.Module):
    ''' Multi-Head Attention module '''

    def __init__(self, max_seq_len, batch_size, n_head, d_model, d_k, d_v, attn_type, dropout=0.1):
        super().__init__()

        self.n_head = n_head
        self.batch_size = batch_size
        self.max_seq_len = max_seq_len
        
        self.d_k = d_k
        self.d_v = d_v
        self.d_model = d_model
        
        self.w_qs = nn.Linear(d_model, n_head * d_k, bias=False)
        self.w_vs = nn.Linear(d_model, n_head * d_v, bias=False)
        self.attn_type = attn_type.lower()
        self._init_attn()
        
        self.fc = nn.Linear(n_head * d_v, d_model, bias=False)
                

        self.dropout = nn.Dropout(dropout)
        self.layer_norm = nn.LayerNorm(d_model, eps=1e-6)

    def _init_attn(self, ):
        # print(self.attn_type)
        if self.attn_type == "vanilla":
            self.w_ks = nn.Linear(self.d_model, self.n_head * self.d_k, bias=False)
            self.attention = ScaledDotProductAttention(temperature=self.d_k ** 0.5)
        elif self.attn_type == "dense":
            self.attention = DenseAttention(self.max_seq_len, self.d_k,)
        elif self.attn_type == "random":
            self.attention = RandomAttention(self.batch_size, self.n_head, self.max_seq_len,)


    def forward(self, q, k, v, mask=None, factorize=False):

        d_k, d_v, n_head = self.d_k, self.d_v, self.n_head
        sz_b, len_q, len_k, len_v = q.size(0), q.size(1), k.size(1), v.size(1)

        residual = q

        # Pass through the pre-attention projection: b x lq x (n*dv)
        # Separate different heads: b x lq x n x dv
        q = self.w_qs(q).view(sz_b, len_q, n_head, d_k)
        v = self.w_vs(v).view(sz_b, len_v, n_head, d_v)

        # Transpose for attention dot product: b x n x lq x dv
        q, v = q.transpose(1, 2), v.transpose(1, 2)

        # For head axis broadcasting.
        if mask is not None:
            mask = mask.unsqueeze(1) 

        # Attention type specific input pre-processing 
        
        if self.attn_type == "vanilla":
            k = self.w_ks(k).view(sz_b, len_k, n_head, d_k)
            k = k.transpose(1, 2)
            q, attn = self.attention(q, k, v, mask=mask)

        elif self.attn_type == "dense":
            q, attn = self.attention(q, v, len_q, mask=mask)
        elif self.attn_type == "random":
            q, attn = self.attention(v, len_q, mask=mask)

        # elif self.atten_type == "Random":

        # elif self.atten_type == "CNN":


        # Transpose to move the head dimension back: b x lq x n x dv
        # Combine the last two dimensions to concatenate all the heads together: b x lq x (n*dv)

        q = q.transpose(1, 2).contiguous().view(sz_b, len_q, -1)
        # print('q: ', q.shape)
        # q = q.contiguous()
        # print('q_2: ', q.shape)
        # print('okkk: ', sz_b, len_q)
        # q = q.view(sz_b, len_q, -1)
        
        q = self.dropout(self.fc(q))
        q += residual

        q = self.layer_norm(q)

        return q, attn


class PositionwiseFeedForward(nn.Module):
    ''' A two-feed-forward-layer module '''

    def __init__(self, d_in, d_hid, dropout=0.1):
        super().__init__()
        self.w_1 = nn.Linear(d_in, d_hid) # position-wise
        self.w_2 = nn.Linear(d_hid, d_in) # position-wise
        self.layer_norm = nn.LayerNorm(d_in, eps=1e-6)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):

        residual = x

        x = self.w_2(F.relu(self.w_1(x)))
        x = self.dropout(x)
        x += residual

        x = self.layer_norm(x)

        return x

class DecoderLayer(nn.Module):
    ''' Compose with three layers '''

    def __init__(self, max_seq_len, batch_size, d_model, d_inner, n_head, d_k, d_v, attn_type, dropout=0.1):
        super(DecoderLayer, self).__init__()
        self.slf_attn = MultiHeadAttention(max_seq_len, batch_size, n_head, d_model, d_k, d_v, attn_type, dropout=dropout)
        self.enc_attn = MultiHeadAttention(max_seq_len, batch_size, n_head, d_model, d_k, d_v, attn_type='vanilla', dropout=dropout)
        self.pos_ffn = PositionwiseFeedForward(d_model, d_inner, dropout=dropout)

    def forward(
            self, dec_input, enc_output,
            slf_attn_mask=None, dec_enc_attn_mask=None):
        dec_output, dec_slf_attn = self.slf_attn(
            dec_input, dec_input, dec_input, mask=slf_attn_mask)
        dec_output, dec_enc_attn = self.enc_attn(
            dec_output, enc_output, enc_output, mask=dec_enc_attn_mask)
        dec_output = self.pos_ffn(dec_output)
        return dec_output, dec_slf_attn, dec_enc_attn