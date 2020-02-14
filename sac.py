import copy
import itertools
import math
import random
from collections import deque

import torch
import torch.nn as nn
from torch.distributions import Normal, TransformedDistribution
from torch.distributions.transforms import AffineTransform, SigmoidTransform
from torch.utils.data import DataLoader

from .utils import SACDataSet, Transition, ReplayPool


class MLPNetwork(nn.Module):
    
    def __init__(self, input_dim, output_dim, hidden_size=256):
        super(MLPNetwork, self).__init__()
        self.network = nn.Sequential(
                        nn.Linear(input_dim, hidden_size),
                        nn.ReLU(),
                        nn.Linear(hidden_size, hidden_size),
                        nn.ReLU(),
                        nn.Linear(hidden_size, hidden_size),
                        nn.ReLU(),
                        nn.Linear(hidden_size, output_dim),
                        )
    
    def forward(self, x):
        return self.network(x)


class Policy(nn.Module):

    def __init__(self, state_dim, action_dim, hidden_size=256):
        super(Policy, self).__init__()
        self.action_dim = action_dim
        self.network = MLPNetwork(state_dim, action_dim * 2, hidden_size)

    def forward(self, x):
        mu_logvar = self.network(x)
        mu, logvar = mu_logvar[:self.action_dim], mu_logvar[self.action_dim:]
        std = torch.exp(0.5 * logvar)
        dist = Normal(mu, std)
        # tanh transform (2 * sigmoid(2x) - 1)
        transforms = [AffineTransform(loc=0, scale=2), SigmoidTransform(), AffineTransform(loc=-1, scale=2)]
        dist = TransformedDistribution(dist, transforms)
        action = dist.rsample()
        logprob = dist.log_prob(action)
        return action, logprob

    def get_action(self, x):
        action, _ = self(x)
        return action


class DoubleQFunc(nn.Module):
    
    def __init__(self, state_dim, hidden_size=256):
        super(DoubleQFunc, self).__init__()
        self.network1 = MLPNetwork(state_dim, 1, hidden_size)
        self.network2 = MLPNetwork(state_dim, 1, hidden_size)

    def forward(self, x):
        return self.network1(x), self.network2(x)


class SAC_Agent:

    def __init__(self, seed, state_dim, action_dim, lr=3e-4, gamma=0.99, epochs=1, tau=5e-3, batchsize=256, update_interval=1):
        self.gamma = gamma
        self.epochs = epochs
        self.tau = tau
        self.target_entropy = -action_dim
        self.batchsize = batchsize
        self.update_interval = update_interval

        torch.manual_seed(seed)

        # aka critic
        self.q_funcs = DoubleQFunc(state_dim + action_dim)
        self.target_q_funcs = copy.deepcopy(self.q_funcs)
        self.target_q_funcs.eval()

        # aka actor
        self.policy = MLPNetwork(state_dim, action_dim)

        # aka temperature
        self.log_alpha = torch.zeros(1, requires_grad=True)

        self.q_optimizers = torch.optim.Adam(self.q_funcs.parameters(), lr=lr)
        self.policy_optimizer = torch.optim.Adam(self.policy.parameters(), lr=lr)
        self.temp_optimizer = torch.optim.Adam(self.log_alpha, lr=lr)

        self.replay_pool = ReplayPool()

    def update_target(self):
        """moving average update of target networks"""
        for target_q_func, q_func in zip(self.target_q_funcs, self.q_funcs):
            for target_q_param, q_param in zip(target_q_func.parameters(), q_func.parameters()):
                target_q_param.data.copy_(self.tau * q_param.data + (1 - self.tau) * target_q_param.data)

    def update_q_functions(self, state_batch, action_batch, reward_batch, nextstate_batch):
        alpha = self.log_alpha.exp().item()
        nextaction_batch, logprobs_batch = self.policy(nextstate_batch)
        nextsa_batch = torch.cat(nextstate_batch, nextaction_batch, dim=1)
        sa_batch = torch.cat(state_batch, action_batch, dim=1)
        with torch.no_grad:
            q_targets = self.target_q_funcs(nextsa_batch)
            # take min to mitigate positive bias in q-function training
            q_targets = torch.min(q_targets, dim=0)
        q_values = torch.cat(self.q_funcs(sa_batch), dim=1)
        loss = 0.5 * (q_values - reward_batch + self.gamma * (q_targets - alpha * logprobs_batch)).pow(2).mean()
        self.q_optimizers.zero_grad()
        loss.backward()
        self.q_optimizers.step()

    def update_policy_and_temp(self, state_batch):
        alpha = self.log_alpha.exp().item()
        action_batch, logprobs_batch = self.policy(state_batch)
        stateaction_batch = torch.cat(state_batch, action_batch)
        with torch.no_grad:
            self.q_funcs.eval()
            qval_batch = torch.cat(self.q_funcs(stateaction_batch))
            qval_batch = torch.min(qval_batch, dim=0)
            self.q_funcs.train()
        policy_loss = (alpha * logprobs_batch - qval_batch).mean()
        self.policy_optimizer.zero_grad()
        policy_loss.backward()
        self.policy_optimizer.step()
        temp_loss = -self.log_alpha * (logprobs_batch.detach() + self.target_entropy).mean()
        self.temp_optimizer.zero_grad()
        temp_loss.backward()
        self.temp_optimizer.step()

    def optimize(self):
        train_data = self.replay_pool.get_all()
        dataset = SACDataSet(train_data)
        dataloader = DataLoader(dataset,
                        shuffle=True,
                        batch_size=self.batchsize,
                        pin_memory=True)
        
        for _ in self.epochs:
            for i, (state_batch, action_batch, reward_batch, nextstate_batch) in enumerate(dataloader):
                self.update_q_functions(state_batch, action_batch, reward_batch, nextstate_batch)
                self.update_policy_and_temp(state_batch)
                if i // self.update_target == 0:
                    self.update_target()



