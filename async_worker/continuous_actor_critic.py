#######################################################################
# Copyright (C) 2017 Shangtong Zhang(zhangshangtong.cpp@gmail.com)    #
# Permission given to modify the code as long as you keep this        #
# declaration at the top                                              #
#######################################################################
import numpy as np
import torch
from torch.autograd import Variable
import torch.nn as nn

class ContinuousAdvantageActorCritic:
    def __init__(self, config, learning_network, target_network):
        self.config = config
        self.optimizer = config.optimizer_fn(learning_network.parameters())
        self.worker_network = config.network_fn()
        self.worker_network.load_state_dict(learning_network.state_dict())
        self.task = config.task_fn()
        self.policy = config.policy_fn()
        self.learning_network = learning_network

    def episode(self, deterministic=False):
        config = self.config
        state = self.task.reset()
        steps = 0
        total_reward = 0
        pending = []
        pi = Variable(torch.FloatTensor([np.pi]))
        while not config.stop_signal.value and \
                (not config.max_episode_length or steps < config.max_episode_length):
            mean, var, value = self.worker_network.predict(np.stack([state]))
            action = self.policy.sample(mean.data.numpy().flatten(),
                                        var.data.numpy().flatten(),
                                        deterministic)
            next_state, reward, terminal, _ = self.task.step(action)

            steps += 1
            total_reward += reward
            if not deterministic:
                reward = np.clip(reward, -1, 1)

            if deterministic:
                if terminal:
                    break
                state = next_state
                continue

            pending.append([mean, var, value, action, reward])
            with config.steps_lock:
                config.total_steps.value += 1

            if terminal or len(pending) >= config.update_interval:
                loss = 0
                if terminal:
                    R = torch.FloatTensor([[0]])
                else:
                    R = self.worker_network.critic(np.stack([next_state])).data
                GAE = torch.FloatTensor([[0]])
                for i in reversed(range(len(pending))):
                    mean, var, value, action, reward = pending[i]
                    if i == len(pending) - 1:
                        delta = reward + config.discount * R - value.data
                    else:
                        delta = reward + pending[i + 1][2].data - value.data
                    GAE = config.discount * config.gae_tau * GAE + delta

                    action = Variable(torch.FloatTensor([action]))
                    prob_part1 = (-(action - mean).pow(2) / (2 * var)).exp()
                    prob_part2 = 1 / (2 * var * pi.expand_as(var)).sqrt()
                    prob = prob_part1 * prob_part2
                    log_prob = prob.log()
                    loss += -torch.sum(log_prob) * Variable(GAE)
                    entropy = 0.5 * (1.0 + (var * 2 * pi.expand_as(var)).log()).sum()
                    loss += config.entropy_weight * entropy

                    R = reward + config.discount * R
                    loss += 0.5 * (Variable(R) - value).pow(2)

                pending = []
                self.worker_network.zero_grad()
                self.optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm(self.worker_network.parameters(), config.gradient_clip)
                for param, worker_param in zip(
                        self.learning_network.parameters(), self.worker_network.parameters()):
                    if param.grad is not None:
                        break
                    param._grad = worker_param.grad
                self.optimizer.step()
                self.worker_network.load_state_dict(self.learning_network.state_dict())
                self.worker_network.reset(terminal)

            if terminal:
                break
            state = next_state

        return steps, total_reward