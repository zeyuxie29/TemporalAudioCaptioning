import torch
import torch.nn as nn


def init_weights(m):
    if isinstance(m, (nn.Conv2d, nn.Conv1d)):
        nn.init.kaiming_normal_(m.weight)
        if m.bias is not None:
            nn.init.constant_(m.bias, 0)
    elif isinstance(m, nn.BatchNorm2d):
        nn.init.constant_(m.weight, 1)
        if m.bias is not None:
            nn.init.constant_(m.bias, 0)
    elif isinstance(m, nn.Linear):
        nn.init.kaiming_uniform_(m.weight)
        if m.bias is not None:
            nn.init.constant_(m.bias, 0)
    elif isinstance(m, nn.Embedding):
        nn.init.kaiming_uniform_(m.weight)


def generate_length_mask(lens, max_length=None):
    lens = torch.as_tensor(lens)
    N = lens.size(0)
    if max_length is None:
        max_length = max(lens)
    idxs = torch.arange(max_length).repeat(N).view(N, max_length)
    mask = (idxs < lens.view(-1, 1))
    return mask


def sum_with_lens(features, lens):
    lens = torch.as_tensor(lens)
    if max(lens) != features.size(1):
        max_length = features.size(1)
        mask = generate_length_mask(lens, max_length)
    else:
        mask = generate_length_mask(lens)
    mask = mask.to(features.device) # [N, T]

    while mask.ndim < features.ndim:
        mask = mask.unsqueeze(-1)
    feature_masked = features * mask
    feature_sum = feature_masked.sum(1)
    return feature_sum


def mean_with_lens(features, lens):
    """
    features: [N, T, ...] (assume the second dimension represents length)
    lens: [N,]
    """
    feature_sum = sum_with_lens(features, lens)
    while lens.ndim < feature_sum.ndim:
        lens = lens.unsqueeze(1)
    feature_mean = feature_sum / lens.to(features.device)
    return feature_mean


def max_with_lens(features, lens):
    """
    features: [N, T, ...] (assume the second dimension represents length)
    lens: [N,]
    """
    lens = torch.as_tensor(lens)
    mask = generate_length_mask(lens).to(features.device) # [N, T]

    feature_max = features.clone()
    feature_max[~mask] = float("-inf")
    feature_max, _ = feature_max.max(1)
    return feature_max


def linear_softmax_with_lens(features, lens):
    return sum_with_lens(features ** 2, lens) / sum_with_lens(features, lens)


def sum_pool_with_lens(features, lens, pooldim=2):
    lens = torch.as_tensor(lens)
    if max(lens) != features.size(1):
        max_length = features.size(1)
        mask = generate_length_mask(lens, max_length)
    else:
        mask = generate_length_mask(lens)
    mask = mask.to(features.device) # [N, T]

    while mask.ndim < features.ndim:
        mask = mask.unsqueeze(-1)
    feature_masked = features * mask
    feature_sum = feature_masked.sum(pooldim)
    return feature_sum

def linear_softmax_pool_with_lens(features, lens, pooldim=2):
    features_sum = features.sum(pooldim)       
    features_sum = torch.where(features_sum > 0, features_sum, 1)
    return sum_pool_with_lens(features ** 2, lens, pooldim) / features_sum

def mean_by_group(arr, grp_num):
    # arr: [total_len, *]
    # grp_num: [num_group,], sum(grp_num) = total_len
    index = sum([[i] * num for i, num in enumerate(grp_num)], [])
    index = torch.as_tensor(index)

    while index.ndim < arr.ndim:
        index = index.unsqueeze(-1)
    index = index.expand(-1, *arr.shape[1:]).to(arr.device)

    res = torch.zeros(len(grp_num), *arr.shape[1:]).to(arr.device)
    res.scatter_add_(0, index, arr)
    denominator = torch.as_tensor(grp_num).to(res.device)
        
    while denominator.ndim < res.ndim:
        denominator = denominator.unsqueeze(-1)

    res = res / denominator
    return res

