# -*- coding:utf-8 -*-

import numpy as np
import torch
import torch.nn as nn
from copy import deepcopy
from torch.utils.data import Dataset, DataLoader
from sklearn.metrics import recall_score, f1_score, cohen_kappa_score,confusion_matrix
import numpy as np
import torch
from torch.utils.data import DataLoader
import os

def square(x):
    return x * x

def cov(x):
    """
    Compute covariance of a (n x c x h x w) tensor.
    It should produces a (n x c x w x w) tensor.
    """
    x_t = x.permute([0, 1, 3, 2])
    return torch.matmul(x_t, x)


def safe_log(x, eps=1e-6):
    """ Prevents :math:`log(0)` by using :math:`log(max(x, eps))`."""
    return torch.log(torch.clamp(x, min=eps))


def log_cov(x):
    """
    Compute log covariance of a (n x c x h x w) tensor.
    It should produces a (n x c x w x w) tensor.
    """
    return safe_log(cov(x))
    
class Expression(nn.Module):
    """Compute given expression on forward pass.

    Parameters
    ----------
    expression_fn : callable
        Should accept variable number of objects of type
        `torch.autograd.Variable` to compute its output.
    """

    def __init__(self, expression_fn):
        super(Expression, self).__init__()
        self.expression_fn = expression_fn

    def forward(self, *x):
        return self.expression_fn(*x)

    def __repr__(self):
        if hasattr(self.expression_fn, "func") and hasattr(
            self.expression_fn, "kwargs"
        ):
            expression_str = "{:s} {:s}".format(
                self.expression_fn.func.__name__, str(self.expression_fn.kwargs)
            )
        elif hasattr(self.expression_fn, "__name__"):
            expression_str = self.expression_fn.__name__
        else:
            expression_str = repr(self.expression_fn)
        return (
            self.__class__.__name__ +
            "(expression=%s) " % expression_str
        )


class Swish(nn.Module):
    '''
    The swish layer: implements the swish activation function
    '''
    def __init__(self):
        super(Swish, self).__init__()

    def forward(self, x):
        return x * torch.sigmoid(x)


class Conv2dNormWeight(nn.Conv2d):
    def __init__(self, *args, max_norm=1, **kwargs):
        self.max_norm = max_norm
        super(Conv2dNormWeight, self).__init__(*args, **kwargs)

    def forward(self, x):
        self.weight.data = torch.renorm(
            self.weight.data, p=2, dim=0, maxnorm=self.max_norm
        )
        return super(Conv2dNormWeight, self).forward(x)
    

class LinearNormWeight(nn.Linear):
    def __init__(self, *args, max_norm=1, **kwargs):
        self.max_norm = max_norm
        super(LinearNormWeight, self).__init__(*args, **kwargs)

    def forward(self, x):
        self.weight.data = torch.renorm(
            self.weight.data, p=2, dim=0, maxnorm=self.max_norm
        )
        return super(LinearNormWeight, self).forward(x)


class DepthwiseConv2d(nn.Conv2d):
    """
    https://gist.github.com/bdsaglam/
    """
    def __init__(self,
                 in_channels,
                 depth_multiplier=1,
                 kernel_size=3,
                 stride=1,
                 padding=0,
                 dilation=1,
                 bias=True,
                 padding_mode='zeros'
                 ):
        out_channels = in_channels * depth_multiplier
        super().__init__(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=in_channels, # the key point
            bias=bias,
            padding_mode=padding_mode
        )


class SeparableConv2d(nn.Module):
    """
    https://gist.github.com/bdsaglam/
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size=3,
                 stride=1,
                 padding=0,
                 dilation=1,
                 bias=True,
                 padding_mode='zeros',
                 depth_multiplier=1,
        ):
        super().__init__()

        intermediate_channels = in_channels * depth_multiplier
        self.spatialConv = nn.Conv2d(
             in_channels=in_channels,
             out_channels=intermediate_channels,
             kernel_size=kernel_size,
             stride=stride,
             padding=padding,
             dilation=dilation,
             groups=in_channels, # key point 1
             bias=bias,
             padding_mode=padding_mode
        )
        self.pointConv = nn.Conv2d(
             in_channels=intermediate_channels,
             out_channels=out_channels,
             kernel_size=1, # key point 2
             stride=1,
             padding=0,
             dilation=1,
             bias=bias,
             padding_mode=padding_mode,
        )

    def forward(self, x):
        return self.pointConv(self.spatialConv(x))


class RememberBest(object):
    """
    Class to remember and restore 
    the parameters of the model and the parameters of the
    optimizer at the epoch with the best performance.

    Parameters
    ----------
    column_name: str
        The best value in this column should indicate the epoch with the
        best performance (e.g. misclass might make sense).
    order: {1, -1} 
        1 means descend order, that is lower best_value is better, such as misclass.
        -1 means ascend order, that is larger best_value is better, such as accuracy.
        
    Attributes
    ----------
    best_epoch: int
        Index of best epoch
    """

    def __init__(self, column_name, order=1):
        self.column_name = column_name
        self.best_epoch = 0
        if order not in (1, -1):
            assert 'order should be either 1 or -1'
        self.order = order
        self.best_value = order * float("inf")
        self.model_state_dict = None
        self.optimizer_state_dict = None

    def remember_epoch(self, epochs_df, model, optimizer):
        """
        Remember this epoch: Remember parameter values in case this epoch
        has the best performance so far.
        
        Parameters
        ----------
        epochs_df: `pandas.Dataframe`
            Dataframe containing the column `column_name` with which performance
            is evaluated.
        model: `torch.nn.Module`
        optimizer: `torch.optim.Optimizer`

        """
        i_epoch = len(epochs_df) - 1
        current_val = float(epochs_df[self.column_name].iloc[-1])
        if self.order > 0 and current_val <= self.best_value:
            self.best_epoch = i_epoch
            self.best_value = current_val
            self.model_state_dict = deepcopy(model.state_dict())
            self.optimizer_state_dict = deepcopy(optimizer.state_dict())
        elif self.order < 0 and current_val >= self.best_value:
            self.best_epoch = i_epoch
            self.best_value = current_val
            self.model_state_dict = deepcopy(model.state_dict())
            self.optimizer_state_dict = deepcopy(optimizer.state_dict())

    def reset_to_best_model(self, epochs_df, model, optimizer):
        """
        Reset parameters to parameters at best epoch and remove rows 
        after best epoch from epochs dataframe.
        
        Modifies parameters of model and optimizer, changes epochs_df in-place.
        
        Parameters
        ----------
        epochs_df: `pandas.Dataframe`
        model: `torch.nn.Module`
        optimizer: `torch.optim.Optimizer`

        """
        # Remove epochs past the best one from epochs dataframe
        epochs_df.drop(range(self.best_epoch + 1, len(epochs_df)), inplace=True)
        model.load_state_dict(self.model_state_dict)
        optimizer.load_state_dict(self.optimizer_state_dict)


@torch.no_grad()
def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    if target.numel() == 0:
        return [torch.zeros([], device=output.device)]

    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].contiguous().view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


def calculate_metrics(y_true, y_pred):
    np.seterr(divide='ignore', invalid='ignore')
    """Calculate recall, F1-score, Kappa, G-Mean and per-class F1-score"""

    recall = recall_score(y_true, y_pred, average='macro', zero_division=0)
    f1 = f1_score(y_true, y_pred, average='macro')
    kappa = cohen_kappa_score(y_true, y_pred)

    # G-Mean: Geometric mean of sensitivity and specificity
    cm = confusion_matrix(y_true, y_pred)
    sensitivity = np.diag(cm) / np.sum(cm, axis=1)  # True Positive / (True Positive + False Negative)
    sensitivity = np.nan_to_num(sensitivity, nan=0.0)  # Replace NaN with 0

    specificity = np.diag(cm) / np.sum(cm, axis=0)  # True Positive / (True Positive + False Positive)
    specificity = np.nan_to_num(specificity, nan=0.0)  # Replace NaN with 0

    g_mean = np.sqrt(np.mean(sensitivity * specificity))  # Geometric mean

    # Calculate F1-score per class
    class_f1_scores = f1_score(y_true, y_pred, average=None)  # F1 score per class

    # If the number of classes in `class_f1_scores` is less than `n_classes`,
    # pad with 0 to make it the length of `n_classes`
    if len(class_f1_scores) < 5:
        class_f1_scores = np.pad(class_f1_scores, (0, 5 - len(class_f1_scores)), mode='constant', constant_values=0)

    return recall, f1, kappa, g_mean, class_f1_scores


def train_epoch(model, datasource, criterion, optimizer, batch_size=32, device=torch.device("cuda:0" if torch.cuda.is_available() else "cpu")):
    model.train()
    if isinstance(datasource, Dataset):
        train_dataloader = DataLoader(datasource, batch_size=batch_size, shuffle=True)
    elif isinstance(datasource, DataLoader):
        train_dataloader = datasource
    else:
        raise 'Unknown data source type'

    accus = []
    losses = []
    y_true = []
    y_pred = []

    for batch_x, batch_y, batch_posture in train_dataloader:  # 假设体位信息在batch_posture中

        batch_x = batch_x.to(device)
        batch_y = batch_y.to(device)
        batch_posture = batch_posture.to(device)  # 体位信息也要传到GPU上
        batch_yp = model(batch_x, batch_posture)  # 传入体位信息

        loss = criterion(batch_yp, batch_y)
        accu = accuracy(batch_yp, batch_y)[0]

        losses.append(loss.item() / batch_x.shape[0])
        accus.append(accu.item())

        y_true.extend(batch_y.cpu().numpy())
        y_pred.extend(batch_yp.argmax(dim=1).cpu().numpy())

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()


    recall, f1, kappa, g_mean, class_f1_scores = calculate_metrics(y_true, y_pred)

    return np.mean(accus), np.mean(losses), recall, f1, kappa, g_mean, class_f1_scores


def evaluate(model, datasource, criterion, batch_size=12, device=torch.device("cuda:0" if torch.cuda.is_available() else "cpu")):
    model.eval()

    if isinstance(datasource, Dataset):
        valid_dataloader = DataLoader(datasource, batch_size=batch_size, shuffle=False)
    elif isinstance(datasource, DataLoader):
        valid_dataloader = datasource
    else:
        raise 'Unknown data source type'

    accus = []
    losses = []
    y_true = []
    y_pred = []
    y_probs = []  # 用于保存预测概率

    with torch.no_grad():  # 不需要计算梯度
        for batch_x, batch_y, batch_posture in valid_dataloader:  # 假设体位信息在batch_posture中
            batch_x = batch_x.to(device)
            batch_y = batch_y.to(device)
            batch_posture = batch_posture.to(device)  # 体位信息也要传到GPU上

            batch_yp = model(batch_x, batch_posture)  # 传入体位信息
            loss = criterion(batch_yp, batch_y)
            accu = accuracy(batch_yp, batch_y)[0]

            # 保存损失和准确率
            losses.append(loss.item() / batch_x.shape[0])
            accus.append(accu.item())

            # 保存真实标签和预测值
            y_true.extend(batch_y.cpu().numpy())
            y_pred.extend(batch_yp.argmax(dim=1).cpu().numpy())  # 预测的类别
            y_probs.extend(batch_yp.softmax(dim=1).cpu().numpy())  # 预测的概率

    # 计算评估指标
    recall, f1, kappa, g_mean, class_f1_scores = calculate_metrics(y_true, y_pred)

    # 返回评估结果，同时返回真实标签、预测值和概率
    return (
        np.mean(accus),  # 平均准确率
        np.mean(losses),  # 平均损失
        recall,
        f1,
        kappa,
        g_mean,
        class_f1_scores,
        y_pred,  # 返回预测的类别
        y_true,  # 返回真实标签
        y_probs  # 返回预测概率
    )


def save_model(save_dir, epoch, model, optimizer, lr_scheduler, device_ids, best=False):
    # 确保目录存在
    os.makedirs(save_dir, exist_ok=True)

    state = {
        'epoch': epoch,
        'state_dict': model.state_dict() if len(device_ids) <= 1 else model.module.state_dict(),
        'optimizer': optimizer.state_dict(),
        'lr_scheduler': lr_scheduler.state_dict() if lr_scheduler is not None else None,
    }

    if best:
        filename = os.path.join(save_dir, 'best.pth')
        torch.save(state, filename)


if __name__ == '__main__':

    x = torch.randn([10, 3, 4, 5])
    xx = Expression(cov)(x)
    print(xx.shape)