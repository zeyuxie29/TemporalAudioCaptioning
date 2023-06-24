# -*- coding: utf-8 -*-

import math
import copy

import torch
import torch.nn as nn
import torch.nn.functional as F

from captioning.models.utils import mean_with_lens, max_with_lens, init, pack_wrapper, generate_length_mask

class BaseEncoder(nn.Module):
    
    """
    Encode the given audio into embedding
    Base encoder class, cannot be called directly
    All encoders should inherit from this class
    """

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim):
        super(BaseEncoder, self).__init__()
        self.raw_feat_dim = raw_feat_dim
        self.fc_feat_dim = fc_feat_dim
        self.attn_feat_dim = attn_feat_dim


    def forward(self, x):
        #########################
        # an encoder first encodes audio feature into embedding, obtaining
        # `encoded`: {
        #     fc_embs: [N, fc_emb_dim],
        #     attn_embs: [N, attn_max_len, attn_emb_dim],
        #     attn_emb_lens: [N,]
        # }
        #########################
        raise NotImplementedError


class Block2D(nn.Module):

    def __init__(self, cin, cout, kernel_size=3, padding=1):
        super().__init__()
        self.block = nn.Sequential(
            nn.BatchNorm2d(cin),
            nn.Conv2d(cin,
                      cout,
                      kernel_size=kernel_size,
                      padding=padding,
                      bias=False),
            nn.LeakyReLU(inplace=True, negative_slope=0.1))

    def forward(self, x):
        return self.block(x)


class LinearSoftPool(nn.Module):
    """LinearSoftPool
    Linear softmax, takes logits and returns a probability, near to the actual maximum value.
    Taken from the paper:
        A Comparison of Five Multiple Instance Learning Pooling Functions for Sound Event Detection with Weak Labeling
    https://arxiv.org/abs/1810.09050
    """
    def __init__(self, pooldim=1):
        super().__init__()
        self.pooldim = pooldim

    def forward(self, logits, time_decision):
        return (time_decision**2).sum(self.pooldim) / time_decision.sum(
            self.pooldim)


class MeanPool(nn.Module):

    def __init__(self, pooldim=1):
        super().__init__()
        self.pooldim = pooldim

    def forward(self, logits, decision):
        return torch.mean(decision, dim=self.pooldim)


class AttentionPool(nn.Module):  
    """docstring for AttentionPool"""  
    def __init__(self, inputdim, outputdim=10, pooldim=1, **kwargs):  
        super().__init__()  
        self.inputdim = inputdim  
        self.outputdim = outputdim  
        self.pooldim = pooldim  
        self.transform = nn.Linear(inputdim, outputdim)  
        self.activ = nn.Softmax(dim=self.pooldim)  
        self.eps = 1e-7  

    def forward(self, logits, decision):  
        # Input is (B, T, D)  
        # B, T , D  
        w = self.activ(torch.clamp(self.transform(logits), -15, 15))  
        detect = (decision * w).sum(  
            self.pooldim) / (w.sum(self.pooldim) + self.eps)  
        # B, T, D  
        return detect


class MMPool(nn.Module):

    def __init__(self, dims):
        super().__init__()
        self.avgpool = nn.AvgPool2d(dims)
        self.maxpool = nn.MaxPool2d(dims)

    def forward(self, x):
        return self.avgpool(x) + self.maxpool(x)


def parse_poolingfunction(poolingfunction_name='mean', **kwargs):
    """parse_poolingfunction
    A heler function to parse any temporal pooling
    Pooling is done on dimension 1
    :param poolingfunction_name:
    :param **kwargs:
    """
    poolingfunction_name = poolingfunction_name.lower()
    if poolingfunction_name == 'mean':
        return MeanPool(pooldim=1)
    elif poolingfunction_name == 'linear':
        return LinearSoftPool(pooldim=1)
    elif poolingfunction_name == 'attention':  
        return AttentionPool(inputdim=kwargs['inputdim'],  
                             outputdim=kwargs['outputdim'])

def embedding_pooling(x, lens, pooling="mean"):
    if pooling == "max":
        fc_embs = max_with_lens(x, lens)
    elif pooling == "mean":
        fc_embs = mean_with_lens(x, lens)
    elif pooling == "mean+max":
        x_mean = mean_with_lens(x, lens)
        x_max = max_with_lens(x, lens)
        fc_embs = x_mean + x_max
    elif pooling == "last":
        indices = (lens - 1).reshape(-1, 1, 1).repeat(1, 1, x.size(-1))
        # indices: [N, 1, hidden]
        fc_embs = torch.gather(x, 1, indices).squeeze(1)
    else:
        raise Exception(f"pooling method {pooling} not support")
    return fc_embs


class Cdur5Encoder(BaseEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, pooling="mean"):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.pooling = pooling
        self.features = nn.Sequential(
            Block2D(1, 32),
            nn.LPPool2d(4, (2, 4)),
            Block2D(32, 128),
            Block2D(128, 128),
            nn.LPPool2d(4, (2, 4)),
            Block2D(128, 128),
            Block2D(128, 128),
            nn.LPPool2d(4, (1, 4)),
            nn.Dropout(0.3),
        )
        with torch.no_grad():
            rnn_input_dim = self.features(
                torch.randn(1, 1, 500, raw_feat_dim)).shape
            rnn_input_dim = rnn_input_dim[1] * rnn_input_dim[-1]

        self.gru = nn.GRU(rnn_input_dim,
                          128,
                          bidirectional=True,
                          batch_first=True)
        self.apply(init)

    def forward(self, input_dict):
        x = input_dict["raw_feats"]
        lens = input_dict["raw_feat_lens"]
        if "upsample" not in input_dict:
            input_dict["upsample"] = False
        lens = torch.as_tensor(copy.deepcopy(lens))
        N, T, _ = x.shape
        x = x.unsqueeze(1)
        x = self.features(x)
        x = x.transpose(1, 2).contiguous().flatten(-2)
        x, _ = self.gru(x)
        if input_dict["upsample"]:
            x = nn.functional.interpolate(
                x.transpose(1, 2),
                T,
                mode='linear',
                align_corners=False).transpose(1, 2)
        else:
            lens //= 4
        attn_embs = x
        fc_embs = embedding_pooling(x, lens, self.pooling)
        return {
            "attn_embs": attn_embs,
            "fc_embs": fc_embs,
            "attn_emb_lens": lens
        }


def conv_conv_block(in_channel, out_channel):
    return nn.Sequential(
        nn.Conv2d(in_channel,
                  out_channel,
                  kernel_size=3,
                  bias=False,
                  padding=1),
        nn.BatchNorm2d(out_channel),
        nn.ReLU(True),
        nn.Conv2d(out_channel,
                  out_channel,
                  kernel_size=3,
                  bias=False,
                  padding=1),
        nn.BatchNorm2d(out_channel),
        nn.ReLU(True)
    )


class Cdur8Encoder(BaseEncoder):
    
    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, pooling="mean"):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.pooling = pooling
        self.features = nn.Sequential(
            conv_conv_block(1, 64),
            MMPool((2, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(64, 128),
            MMPool((2, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(128, 256),
            MMPool((1, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(256, 512),
            MMPool((1, 2)),
            nn.Dropout(0.2, True),
            nn.AdaptiveAvgPool2d((None, 1)),
        )
        self.init_bn = nn.BatchNorm2d(raw_feat_dim)
        self.embedding = nn.Linear(512, 512)
        self.gru = nn.GRU(512, 256, bidirectional=True, batch_first=True)
        self.apply(init)

    def forward(self, input_dict):
        x = input_dict["raw_feats"]
        lens = input_dict["raw_feat_lens"]
        lens = torch.as_tensor(copy.deepcopy(lens))
        x = x.unsqueeze(1)  # B x 1 x T x D
        x = x.transpose(1, 3)
        x = self.init_bn(x)
        x = x.transpose(1, 3)
        x = self.features(x)
        x = x.transpose(1, 2).contiguous().flatten(-2)
        x = F.dropout(x, p=0.5, training=self.training)
        x = F.relu_(self.embedding(x))
        x, _ = self.gru(x)
        attn_embs = x
        lens //= 4
        fc_embs = embedding_pooling(x, lens, self.pooling)
        return {
            "attn_embs": attn_embs,
            "fc_embs": fc_embs,
            "attn_emb_lens": lens
        }


class Cnn10AddinfoEncoder(BaseEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, add_vocab_size):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.features = nn.Sequential(
            conv_conv_block(1, 64),
            nn.AvgPool2d((2, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(64, 128),
            nn.AvgPool2d((2, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(128, 256),
            nn.AvgPool2d((2, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(256, 512),
            nn.AvgPool2d((2, 2)),
            nn.Dropout(0.2, True),
            nn.AdaptiveAvgPool2d((None, 1)),
        )
        self.init_bn = nn.BatchNorm2d(raw_feat_dim)
        self.embedding = nn.Linear(512, 512)
        self.apply(init)
        self.outputlayer_add_info = nn.Linear(512, add_vocab_size)

    def forward(self, input_dict):
        x = input_dict["raw_feats"]
        lens = input_dict["raw_feat_lens"]
        lens = torch.as_tensor(copy.deepcopy(lens))
        x = x.unsqueeze(1)  # [N, 1, T, D]
        x = x.transpose(1, 3)
        x = self.init_bn(x)
        x = x.transpose(1, 3)
        x = self.features(x) # [N, 512, T/16, 1]
        x = x.transpose(1, 2).contiguous().flatten(-2) # [N, T/16, 512]
        attn_embs = x
        lens //= 16
        fc_embs = embedding_pooling(x, lens, "mean+max")
        fc_embs = F.dropout(fc_embs, p=0.5, training=self.training)
        fc_embs = self.embedding(fc_embs)
        fc_embs = F.relu_(fc_embs)
        add_info_embs = self.outputlayer_add_info(fc_embs)
        return {
            "attn_embs": attn_embs,
            "fc_embs": fc_embs,
            "attn_emb_lens": lens,
            "add_info_embs": add_info_embs
        }
class Cnn10Encoder(BaseEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.features = nn.Sequential(
            conv_conv_block(1, 64),
            nn.AvgPool2d((2, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(64, 128),
            nn.AvgPool2d((2, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(128, 256),
            nn.AvgPool2d((2, 2)),
            nn.Dropout(0.2, True),
            conv_conv_block(256, 512),
            nn.AvgPool2d((2, 2)),
            nn.Dropout(0.2, True),
            nn.AdaptiveAvgPool2d((None, 1)),
        )
        self.init_bn = nn.BatchNorm2d(raw_feat_dim)
        self.embedding = nn.Linear(512, 512)
        self.apply(init)

    def forward(self, input_dict):
        x = input_dict["raw_feats"]
        lens = input_dict["raw_feat_lens"]
        lens = torch.as_tensor(copy.deepcopy(lens))
        x = x.unsqueeze(1)  # [N, 1, T, D]
        x = x.transpose(1, 3)
        x = self.init_bn(x)
        x = x.transpose(1, 3)
        x = self.features(x) # [N, 512, T/16, 1]
        x = x.transpose(1, 2).contiguous().flatten(-2) # [N, T/16, 512]
        attn_embs = x
        lens //= 16
        fc_embs = embedding_pooling(x, lens, "mean+max")
        fc_embs = F.dropout(fc_embs, p=0.5, training=self.training)
        fc_embs = self.embedding(fc_embs)
        fc_embs = F.relu_(fc_embs)
        return {
            "attn_embs": attn_embs,
            "fc_embs": fc_embs,
            "attn_emb_lens": lens
        }


class RnnEncoder(BaseEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, **kwargs):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.pooling = "mean+max"
        self.hidden_size = kwargs.get('hidden_size', 512)
        self.bidirectional = kwargs.get('bidirectional', False)
        self.num_layers = kwargs.get('num_layers', 1)
        self.dropout = kwargs.get('dropout', 0.2)
        self.rnn_type = kwargs.get('rnn_type', "GRU")
        self.in_bn = kwargs.get('in_bn', False)
        self.embed_dim = self.hidden_size * (self.bidirectional + 1)
        self.network = getattr(nn, self.rnn_type)(
            attn_feat_dim,
            self.hidden_size,
            num_layers=self.num_layers,
            bidirectional=self.bidirectional,
            dropout=self.dropout,
            batch_first=True)
        if self.in_bn:
            self.bn = nn.BatchNorm1d(self.embed_dim)
        self.apply(init)

    def forward(self, input_dict):
        x = input_dict["attn_feats"]
        lens = input_dict["attn_feat_lens"]
        lens = torch.as_tensor(lens)
        # x: [N, T, E]
        if self.in_bn:
            x = pack_wrapper(self.bn, x, lens)
        out = pack_wrapper(self.network, x, lens)
        # out: [N, T, hidden]
        attn_embs = out
        fc_embs = embedding_pooling(out, lens, self.pooling)
        return {
            "attn_embs": attn_embs,
            "fc_embs": fc_embs,
            "attn_emb_lens": lens
        }


class RnnEncoder2(RnnEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, **kwargs):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim, **kwargs)

    def forward(self, input_dict):
        output = super().forward(input_dict)
        output["fc_embs"] = input_dict["fc_feats"]
        return output


class RnnEncoder3(RnnEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, **kwargs):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim, **kwargs)
        self.network = getattr(nn, self.rnn_type)(
            fc_feat_dim + attn_feat_dim,
            self.hidden_size,
            num_layers=self.num_layers,
            bidirectional=self.bidirectional,
            dropout=self.dropout,
            batch_first=True)
        self.fc_transform = nn.Linear(self.embed_dim, self.embed_dim)
        self.apply(init)

    def forward(self, input_dict):
        fc_feats = input_dict["fc_feats"]
        attn_feats = input_dict["attn_feats"]
        lens = input_dict["attn_feat_lens"]
        lens = torch.as_tensor(lens)
        x = torch.cat((fc_feats.unsqueeze(1).repeat(1, attn_feats.size(1), 1), attn_feats), dim=-1)
        packed = nn.utils.rnn.pack_padded_sequence(x, lens, batch_first=True, enforce_sorted=False)
        packed_out, hid = self.network(packed)
        # hid: [num_layers, N, hidden]
        out, _ = nn.utils.rnn.pad_packed_sequence(packed_out, batch_first=True)
        # out: [N, T, hidden]
        attn_embs = out
        fc_embs = embedding_pooling(out, lens, "mean+max")
        fc_embs = F.dropout(fc_embs, p=0.5, training=self.training)
        fc_embs = self.fc_transform(fc_embs)
        if self.out_bn:
            fc_embs = self.bn(fc_embs)
        return {
            "attn_embs": attn_embs,
            "fc_embs": fc_embs,
            "attn_emb_lens": lens
        }

class RnnEncoder4(RnnEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, input_dim, **kwargs):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim, **kwargs)
        self.network = getattr(nn, self.rnn_type)(
            input_dim,
            self.hidden_size,
            num_layers=self.num_layers,
            bidirectional=self.bidirectional,
            dropout=self.dropout,
            batch_first=True)
        self.init_norm = nn.LayerNorm(input_dim)
        self.fc_in_proj = nn.Linear(fc_feat_dim, input_dim)
        self.attn_in_proj = nn.Linear(attn_feat_dim, input_dim)
        self.fc_out_transform = nn.Linear(self.embed_dim, self.embed_dim)
        if hasattr(self, "bn"):
            del self.bn
        self.out_norm = nn.LayerNorm(self.embed_dim)
        self.apply(init)

    def forward(self, input_dict):
        fc_feats = input_dict["fc_feats"]
        attn_feats = input_dict["attn_feats"]
        lens = input_dict["attn_feat_lens"]
        lens = torch.as_tensor(lens)
        p_fc_feats = self.fc_in_proj(fc_feats)
        p_attn_feats = self.attn_in_proj(attn_feats)
        x = p_fc_feats.unsqueeze(1) + p_attn_feats
        x = self.init_norm(x)
        x = F.dropout(x, p=0.5, training=self.training)
        packed = nn.utils.rnn.pack_padded_sequence(x, lens, batch_first=True, enforce_sorted=False)
        packed_out, hid = self.network(packed)
        # hid: [num_layers, N, hidden]
        out, _ = nn.utils.rnn.pad_packed_sequence(packed_out, batch_first=True)
        # out: [N, T, hidden]
        attn_embs = out
        fc_embs = embedding_pooling(out, lens, "mean+max")
        fc_embs = F.dropout(fc_embs, p=0.5, training=self.training)
        fc_embs = self.fc_out_transform(fc_embs)
        fc_embs = F.relu_(fc_embs)
        fc_embs = F.dropout(fc_embs, p=0.5, training=self.training)
        fc_embs = self.out_norm(fc_embs)
        return {
            "attn_embs": attn_embs,
            "fc_embs": fc_embs,
            "attn_emb_lens": lens
        }

class RnnTemporalEncoder(RnnEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, clf_dim, **kwargs):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim, num_layers=2, **kwargs)
        self.clf = nn.Linear(512, clf_dim)
        self.apply(init)

    def forward(self, input_dict):
        output = super().forward(input_dict)
        output["temporal_pred"] = torch.softmax(self.clf(output["fc_embs"]), -1)
        return output

class FcTemporalEncoder(BaseEncoder):
    
    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, clf_dim, **kwargs):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.clf = nn.Linear(fc_feat_dim, clf_dim)
        self.apply(init)

    def forward(self, input_dict):
        output = {"temporal_pred":  torch.softmax(self.clf(input_dict["fc_feats"]), -1)}
        return output

class CnnTemporalEncoder(BaseEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, clf_dim, **kwargs):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.clfs = nn.Sequential(
            nn.Conv1d(1, 4, kernel_size=3, padding=1),
            nn.Dropout(0.2, True),
            nn.Conv1d(4, 1, kernel_size=3, padding=1),
            nn.Dropout(0.2, True),
            nn.Linear(fc_feat_dim, clf_dim),
        )
        self.apply(init)

    def forward(self, input_dict):
        output = {"temporal_pred":  self.clfs(input_dict["fc_feats"].unsqueeze(1)).squeeze(-1).squeeze(-1)}
        return output

class PositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=100):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer("pe", pe)

    def forward(self, x):
        # x: [T, N, E]
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)


class TransformerEncoder(BaseEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, d_model, **kwargs):
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.d_model = d_model
        dropout = kwargs.get("dropout", 0.2)
        self.nhead = kwargs.get("nhead", self.d_model // 64)
        self.nlayers = kwargs.get("nlayers", 2)
        self.dim_feedforward = kwargs.get("dim_feedforward", self.d_model * 4)

        self.attn_proj = nn.Sequential(
            nn.Linear(attn_feat_dim, self.d_model),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.LayerNorm(self.d_model)
        )
        layer = nn.TransformerEncoderLayer(d_model=self.d_model,
                                           nhead=self.nhead,
                                           dim_feedforward=self.dim_feedforward,
                                           dropout=dropout)
        self.model = nn.TransformerEncoder(layer, self.nlayers)
        self.fc_out_transform = nn.Linear(self.d_model, self.d_model)
        self.init_params()

    def init_params(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, input_dict):
        attn_feats = input_dict["attn_feats"]
        attn_feat_lens = input_dict["attn_feat_lens"]
        attn_feat_lens = torch.as_tensor(attn_feat_lens)

        attn_feats = self.attn_proj(attn_feats) # [bs, T, d_model]
        attn_feats = attn_feats.transpose(0, 1)

        src_key_padding_mask = ~generate_length_mask(attn_feat_lens, attn_feats.size(0)).to(attn_feats.device)
        output = self.model(attn_feats, src_key_padding_mask=src_key_padding_mask)

        attn_embs = output.transpose(0, 1)
        fc_embs = embedding_pooling(attn_embs, attn_feat_lens, "mean+max")
        fc_embs = F.dropout(fc_embs, p=0.5, training=self.training)
        fc_embs = self.fc_out_transform(fc_embs)
        fc_embs = F.relu_(fc_embs)
        fc_embs = F.dropout(fc_embs, p=0.5, training=self.training)
        return {
            "attn_embs": attn_embs,
            "fc_embs": fc_embs,
            "attn_emb_lens": attn_feat_lens
        }


class M2TransformerEncoder(BaseEncoder):

    def __init__(self, raw_feat_dim, fc_feat_dim, attn_feat_dim, d_model, **kwargs):
        try:
            from m2transformer.models.transformer import MemoryAugmentedEncoder, ScaledDotProductAttentionMemory
        except:
            raise ImportError("meshed-memory-transformer not installed; please run `pip install git+https://github.com/ruotianluo/meshed-memory-transformer.git`")
        super().__init__(raw_feat_dim, fc_feat_dim, attn_feat_dim)
        self.d_model = d_model
        dropout = kwargs.get("dropout", 0.1)
        self.nhead = kwargs.get("nhead", self.d_model // 64)
        self.nlayers = kwargs.get("nlayers", 2)
        self.dim_feedforward = kwargs.get("dim_feedforward", self.d_model * 4)

        self.attn_proj = nn.Linear(attn_feat_dim, self.d_model)
        self.pos_encoder = PositionalEncoding(self.d_model, dropout, 200)
        self.model = MemoryAugmentedEncoder(self.nlayers, 0, self.attn_feat_dim,
                                            d_model=self.d_model,
                                            h=self.nhead,
                                            d_ff=self.dim_feedforward,
                                            dropout=dropout,
                                            attention_module=ScaledDotProductAttentionMemory,
                                            attention_module_kwargs={"m": 40})
        self.init_params()

    def init_params(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)


    def forward(self, input_dict):
        attn_feats = input_dict["attn_feats"]
        attn_embs, attn_emb_mask = self.model(attn_feats)
        fc_embs = attn_embs.mean(-2)
        return {
            "fc_embs": fc_embs,
            "attn_embs": attn_embs,
            "attn_emb_mask": attn_emb_mask
        }


if __name__ == "__main__":
    encoder = Cnn10Encoder(64, -1, -1)
    # encoder = RnnEncoder(64, -1, 512, pooling="mean+max")
    print(encoder)
    raw_feats = torch.randn(4, 1571, 64)
    raw_feat_lens = torch.tensor([1071, 666, 1571, 985])
    attn_feats = torch.randn(4, 78, 512)
    attn_feat_lens = torch.tensor([70, 78, 65, 55])
    input_dict = {
        "raw_feats": raw_feats,
        # "fc_feats": None,
        "attn_feats": attn_feats,
        "attn_feat_lens": attn_feat_lens,
        "raw_feat_lens": raw_feat_lens
    }
    output_dict = encoder(input_dict)
    print("attn embs: ", output_dict["attn_embs"].shape)
    print("fc embs: ", output_dict["fc_embs"].shape)
    print("attn emb lens: ", output_dict["attn_emb_lens"])
