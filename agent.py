# Implementation of Reinforcement Learning agents that interact with the
# educational domain environment implemented in Racket.

import argparse
import collections
import copy
from dataclasses import dataclass
import time
import itertools
import random
import json
import math
import subprocess
import logging
import numpy as np

import torch
from torch import nn
from torch.nn import functional as F
from torch.distributions.categorical import Categorical
import wandb

import util
from util import *
from environment import Environment, State, Action
from evaluation import EnvironmentWithEvaluationProxy, evaluate_policy, evaluate_policy_checkpoints
from q_function import QFunction, InverseLength, RandomQFunction, RubiksGreedyHeuristic


SUCCESS_STATE = State(['success'], [], 1.0)


class LearningAgent:
    '''Algorithm that guides learning via interaction with the enviroment.
    Gets to decide when to start a new problem, what states to expand, when to take
    random actions, etc.

    Any learning algorithm can be combined with any Q-Function.
    '''

    subtypes: dict = {}

    def learn_from_environment(self, environment):
        "Lets the agent learn by interaction using any algorithm."
        raise NotImplementedError()

    def learn_from_experience(self):
        "Lets the agent optionally learn from its past interactions one last time before eval."

    def stats(self):
        "Returns a string with learning statistics for this agent, for debugging."
        return ""

    def get_q_function(self):
        "Returns a QFunction that encodes the current learned model."
        raise NotImplementedError()

    @staticmethod
    def new(q_fn, config):
        return LearningAgent.subtypes[config['type']](q_fn, config)


@dataclass
class ContrastiveExample:
    "Keeps track of one contrastive example (one positive vs N negative actions)"
    positive: Action
    negatives: list[Action]
    gap: int  # How many steps into the future is this example for.


@register(LearningAgent)
class NCE(LearningAgent):
    "Agent that uses the InfoNCE contrastive loss to differentiate positive/negative actions"
    def __init__(self, q_function, config):
        self.q_function = q_function
        self.bootstrapping = True if not config['q_function'].get('load_pretrained') else False
        replay_buffer_size = config.get('replay_buffer_size', 10**6)
        self.examples = collections.deque(maxlen=replay_buffer_size)

        self.training_problems_solved = 0
        self.training_acc_moving_average = 0.0

        self.max_depth = config['max_depth']
        self.depth_step = config['depth_step']
        self.initial_depth = config['initial_depth']
        self.step_every = config['step_every']
        self.beam_size = config['beam_size']
        self.corrupt = config['corrupt']

        self.optimize_every = config.get('optimize_every', 1)
        self.n_gradient_steps = config.get('n_gradient_steps', 64)
        self.beam_negatives_frac = config.get('beam_negatives_frac', 1.0)

        bootstrap_from = config.get('bootstrap_from', 'Random')

        if bootstrap_from == 'InverseLength':
            self.bootstrap_policy = InverseLength(self.q_function.device)
        elif bootstrap_from == 'RubiksGreedyHeuristic':
            self.bootstrap_policy = RubiksGreedyHeuristic(self.q_function.device)
        else:
            self.bootstrap_policy = RandomQFunction(self.q_function.device)

        self.n_bootstrap_problems = config.get('n_bootstrap_problems', 100)

        # Knob: whether to add an artificial 'success' state in the end
        # of the solution in training examples. The idea is that this would align
        # all states that are in the path to a solution closer together.
        self.add_success_state = config.get('add_success_state', False)
        self.keep_optimizer = config.get('keep_optimizer', True)
        # Knob: how many future states to use as examples.
        self.n_future_states = config.get('n_future_states', 1)
        self.learning_rate = config.get('lr', 1e-4)
        self.reset_optimizer()

        self.current_depth = self.initial_depth
        self.bootstrapping = True

    def reset_optimizer(self):
        self.optimizer = torch.optim.Adam(self.q_function.parameters(), lr=self.learning_rate)

    def name(self):
        return 'NCE'

    def learn_from_environment(self, environment):
        for i in itertools.count():
            problem = environment.generate_new()
            solution = self.beam_search(problem, environment)

            if solution is not None:
                self.training_problems_solved += 1

                if self.training_problems_solved >= self.n_bootstrap_problems:
                    self.bootstrapping = False

                if self.training_problems_solved % self.optimize_every == 0:
                    logging.info('Running SGD steps.')
                    self.gradient_steps()

            self.training_acc_moving_average = 0.95*self.training_acc_moving_average + 0.05*int(solution is not None)

            if (i + 1) % self.step_every == 0:
                self.current_depth = min(self.max_depth, self.current_depth + self.depth_step)
                logging.info(f'Beam search depth increased to {self.current_depth}.')

    def get_q_function(self):
        if self.bootstrapping:
            return self.bootstrap_policy
        return self.q_function

    def beam_search(self, state, environment):
        '''Performs beam search in a train problem while recording particular examples
        in the replay buffer (according to the various knobs in the algorithm, see config)'''

        beam = [state]
        solution = None  # The state that we found that solves the problem.
        q = self.get_q_function()
        seen = {state}
        visited_states = [[state]]  # List of states visited in each iteration (used to retrieve negatives).

        logging.info(f'Trying {state}')

        for i in range(self.current_depth):
            rewards, actions = zip(*environment.step(beam))

            for s, r, state_actions in zip(beam, rewards, actions):
                # Record solution, if found.
                if r:
                    solution = s

            if solution is not None:
                break

            all_actions = [a for state_actions in actions for a in state_actions]

            if not len(all_actions):
                break

            # Query model, sort next states by value, then update beam.
            with torch.no_grad():
                q_values = q(all_actions).tolist()

            for a, v in zip(all_actions, q_values):
                a.value = v

            next_states = []
            for s, state_actions in zip(beam, actions):
                for a in state_actions:
                    ns = a.next_state
                    ns.value = q.aggregate(s.value, a.value)
                    next_states.append(ns)

            next_states.sort(key=lambda s: s.value, reverse=True)

            # Remove duplicates while keeping the order (i.e. if a state appears multiple times,
            # keep the one with the largest value). Works because dict is ordered in Python 3.6+.
            next_states = [s for s in dict.fromkeys(next_states) if s not in seen]
            
            # corrupt states
            corruption_results = []
            for s in next_states:
                if random.uniform(0, 1) < self.corrupt:
                    corruption_results.append(corrupt_state(s)[1])
                else:
                    corruption_results.append(s)
            next_state = [s for s in corruption_results] 

            # check for errors in corruption
            next_state = [filter_state(s) for s in ns]          

            visited_states.append(next_states)
            seen.update(next_states)
            beam = next_states[:self.beam_size]
            logging.info(f'Beam #{i}: {beam}:')

            if not beam:
                break

        corrupted_answer = False
        if solution is not None:
            if solution.corrupt == True:
                solution = None
                corrupted_answer = True

        logging.info(f'Solved? {solution is not None} (solution len {solution and len(visited_states)}, q={type(q)}, corrupted={corrupted_answer})')

        # If found a solution, make contrastive examples from each iteration.
        if solution is not None:
            positive = solution

            for states in reversed(visited_states):
                if positive.parent_action is None:
                    break

                negatives = [s.parent_action
                             for s in states
                             if s.facts[-1] != positive.facts[-1] and
                             (s.parent_action.state.facts[-1] ==
                                 positive.parent_action.state.facts[-1] or
                                 self.beam_negatives_frac >= random.random())]
                example = ContrastiveExample(positive=positive.parent_action,
                                             negatives=negatives,
                                             gap=1)
                self.examples.append(example)
                positive = positive.parent_action.state

        return solution

    def stats(self):
        return "{} solutions found, {:.4f} training acc".format(
            self.training_problems_solved,
            self.training_acc_moving_average)

    def gradient_steps(self):
        if not self.examples:
            return

        if not self.keep_optimizer:
            self.reset_optimizer()

        celoss = nn.CrossEntropyLoss()
        losses = []

        for i in range(self.n_gradient_steps):
            e = random.choice(self.examples)
            all_actions = [e.positive] + e.negatives

            self.optimizer.zero_grad()
            f_pred = self.q_function(all_actions)
            # Here, the batch is casted as a N + 1-class classification instance,
            # and class 0 is the positive example (by how all_actions is constructed).
            loss = celoss(f_pred.unsqueeze(0), torch.zeros(1, dtype=int, device=f_pred.device))
            # wandb.log({'train_loss': loss.item()})
            losses.append(loss.item())
            loss.backward()
            self.optimizer.step()

        return losses


@register(LearningAgent)
class BeamSearchIterativeDeepening(LearningAgent):
    def __init__(self, q_function, config):
        self.q_function = q_function
        self.bootstrapping = True
        self.replay_buffer_size = config['replay_buffer_size']

        self.replay_buffer_pos = collections.deque(maxlen=self.replay_buffer_size)
        self.replay_buffer_neg = collections.deque(maxlen=self.replay_buffer_size)
        self.training_problems_solved = 0

        self.max_depth = config['max_depth']
        self.depth_step = config['depth_step']
        self.initial_depth = config['initial_depth']
        self.step_every = config['step_every']
        self.beam_size = config['beam_size']
        self.beam_negatives = config.get('beam_negatives', True)

        self.balance_examples = config.get('balance_examples', True)
        self.optimize_on = config.get('optimize_on', 'problem')
        self.reward_decay = config.get('reward_decay', 1.0)
        self.batch_size = config.get('batch_size', 64)
        self.optimize_every = config.get('optimize_every', 1)
        self.n_gradient_steps = config.get('n_gradient_steps', 10)
        self.discard_unsolved_problems = config.get('discard_unsolved', False)
        self.full_imitation_learning = config.get('full_imitation_learning', False)

        if config.get('bootstrap_from', 'Random') == 'InverseLength':
            self.bootstrap_policy = InverseLength(self.q_function.device)
        else:
            self.bootstrap_policy = RandomQFunction(self.q_function.device)

        # Knob: whether to add an artificial 'success' state in the end
        # of the solution in training examples. The idea is that this would align
        # all states that are in the path to a solution closer together.
        self.add_success_state = config.get('add_success_state', False)
        # Knob: how many future states to use as examples.
        self.n_future_states = config.get('n_future_states', 1)
        self.n_negatives = config.get('n_negatives', 1)
        self.learning_rate = config.get('lr', 1e-4)

        self.current_depth = self.initial_depth
        self.bootstrapping = True

    def name(self):
        if self.full_imitation_learning:
            return 'ImitationLearning'
        elif self.depth_step == 0 and not self.balance_examples:
            return 'DAgger'
        elif self.depth_step > 0 and not self.balance_examples:
            return 'IDDagger'
        elif self.depth_step > 0 and self.balance_examples:
            return 'IDCDagger'

    def learn_from_environment(self, environment):
        for i in itertools.count():
            problem = environment.generate_new()
            solution = self.beam_search(problem, environment)

            if solution is not None:
                self.training_problems_solved += 1

            if ((self.optimize_on == 'problem' and (i + 1) % self.optimize_every == 0) or
                (self.optimize_on == 'solution' and solution is not None and
                 self.training_problems_solved % self.optimize_every == 0)):
                logging.info('Running SGD steps.')
                self.gradient_steps()

            if (i + 1) % self.step_every == 0:
                self.current_depth = min(self.max_depth, self.current_depth + self.depth_step)
                logging.info(f'Beam search depth increased to {self.current_depth}.')

    def learn_from_experience(self):
        if self.full_imitation_learning:
            logging.info('Running Imitation learning')
            self.gradient_steps(True)

    def get_q_function(self):
        if self.bootstrapping:
            return self.bootstrap_policy
        return self.q_function

    def beam_search(self, state, environment):
        '''Performs beam search in a train problem while recording particular examples
        in the replay buffer (according to the various knobs in the algorithm, see config)'''

        states_by_id = {id(state): state}
        state_parent_edge = {}
        beam = [state]
        solution = None  # The state that we found that solves the problem.
        action_reward = {}  # Remember rewards we attribute to each action.
        q = self.get_q_function()
        seen = {state}

        logging.info(f'Trying {state}')

        for i in range(self.current_depth):
            rewards, actions = zip(*environment.step(beam))

            for s, r, state_actions in zip(beam, rewards, actions):
                for a in state_actions:
                    # Remember how we got to this state.
                    states_by_id[id(a.next_state)] = a.next_state
                    state_parent_edge[id(a.next_state)] = (s, a)
                # Record solution, if found.
                if r:
                    if self.add_success_state:
                        success = copy.deepcopy(SUCCESS_STATE)
                        a = Action(s, 'success', success, 1.0, 1.0)
                        success.parent_action = a
                        states_by_id[id(success)] = success
                        state_parent_edge[id(success)] = (s, a)
                        solution = [success]
                    else:
                        solution = [s]

            if solution is not None:
                # Traverse all the state -> next_state edges backwards, remembering
                # all states in the path to the solution.
                current = solution[0]
                current_reward = 1.0

                while id(current) in state_parent_edge:
                    prev_s, a = state_parent_edge[id(current)]
                    action_reward[id(a)] = current_reward
                    current_reward *= self.reward_decay
                    current = prev_s
                    solution.append(current)

                solution = list(reversed(solution))
                break

            all_actions = [a for state_actions in actions for a in state_actions]

            if not len(all_actions):
                break

            # Query model, sort next states by value, then update beam.
            with torch.no_grad():
                q_values = q(all_actions).tolist()

            for a, v in zip(all_actions, q_values):
                a.value = v

            next_states = []
            for s, state_actions in zip(beam, actions):
                for a in state_actions:
                    ns = a.next_state
                    ns.value = s.value + np.log(a.value)
                    next_states.append(ns)

            next_states.sort(key=lambda s: s.value, reverse=True)
            # Remove duplicates while keeping the order (i.e. if a state appears multiple times,
            # keep the one with the largest value). Works because dict is ordered in Python 3.6+.
            next_states = [s for s in dict.fromkeys(next_states) if s not in seen]
            seen.update(next_states)
            beam = next_states[:self.beam_size]
            logging.info(f'Beam #{i}: {beam}:')

            if not beam:
                break

        logging.info('Solved? {} (solution len {}, q={})'
                     .format(solution is not None,
                             solution and len(solution),
                             type(q)))

        # Add all edges traversed as examples in the experience replay buffer.
        if solution is not None or not self.discard_unsolved_problems:
            positive_ids = set(id(s) for s in solution) if solution is not None else set()
            # Add negative examples.
            for s, (parent, a) in state_parent_edge.items():
                r = action_reward.get(id(a), 0.0)
                if r == 0 and (self.beam_negatives or id(s) in positive_ids):
                    self.replay_buffer_neg.append((states_by_id[s], a, 0))
            # Add positive examples (possibly looking several steps ahead, depending
            # on `self.n_future_states`.
            if solution is not None:
                for i, s_i in enumerate(solution):
                    for j in range(i+1, min(i + 1 + self.n_future_states, len(solution))):
                        s_j = solution[j]
                        self.replay_buffer_pos.append((states_by_id[s],
                                                       s_j.parent_action,
                                                       action_reward[id(s_j.parent_action)]))

        return None if solution is None else solution[-1]

    def stats(self):
        return "replay buffer size = {}, {} positive".format(
            len(self.replay_buffer_pos) + len(self.replay_buffer_neg),
            len(self.replay_buffer_pos))

    def gradient_steps(self, is_last_round=False):
        if self.full_imitation_learning and not is_last_round:
            return

        if self.balance_examples:
            n_pos = len(self.replay_buffer_pos)
            n_neg = min(self.n_negatives * n_pos, len(self.replay_buffer_neg))
            examples = (random.sample(self.replay_buffer_pos, k=n_pos) +
                        random.sample(self.replay_buffer_neg, k=n_neg))
        else:
            examples = self.replay_buffer_pos + self.replay_buffer_neg

        logging.info(f'Taking {self.n_gradient_steps} with {len(examples)} examples'
                     f' (balanced = {self.balance_examples})')
        batch_size = min(self.batch_size, len(examples))

        if batch_size == 0:
            return

        optimizer = torch.optim.Adam(self.q_function.parameters(), lr=self.learning_rate)

        for i in range(self.n_gradient_steps):
            batch = random.sample(examples, batch_size)
            batch_s, batch_a, batch_r = zip(*batch)

            optimizer.zero_grad()

            r_pred = self.q_function(batch_a)
            loss = F.binary_cross_entropy(r_pred, torch.tensor(batch_r,
                                                               dtype=r_pred.dtype,
                                                               device=r_pred.device))
            wandb.log({'train_loss': loss.item()})
            loss.backward()
            optimizer.step()

        self.bootstrapping = False


# A tuple of the replay buffer. We don't need to store the current state or the next state
# because a0 is an Action object, which already has a0.state and a0.next_state.
QReplayBufferTuple = collections.namedtuple('QReplayBufferTuple',
                                            ['a0', 'r', 'A1'])


@register(LearningAgent)
class QLearning(LearningAgent):
    def __init__(self, q_function, config):
        self.q_function = q_function

        self.replay_buffer_size = config['replay_buffer_size']
        self.max_depth = config['max_depth']

        self.discount_factor = config.get('discount_factor', 1.0)
        self.batch_size = config.get('batch_size', 64)
        self.optimize_every = config.get('optimize_every', 16)
        self.softmax_alpha = config.get('softmax_alpha', 1.0)

        self.replay_buffer = collections.deque(maxlen=self.replay_buffer_size)
        self.solutions_found = 0

        self.optimizer = torch.optim.Adam(q_function.parameters(),
                                          lr=config.get('learning_rate', 1e-4))

    def name(self):
        return 'QLearning'

    def get_q_function(self):
        return self.q_function

    def learn_from_environment(self, environment):
        for i in itertools.count():
            state = environment.generate_new()
            r, actions = environment.step([state])[0]

            if r:
                # Trivial state: already solved, no examples to draw.
                continue

            for j in range(self.max_depth):
                # No actions to take.
                if not len(actions):
                    break

                with torch.no_grad():
                    q_values = self.q_function(actions)
                    pi = Categorical(logits=self.softmax_alpha * q_values)
                    a = pi.sample().item()

                s_next = actions[a].next_state
                r, next_actions = environment.step([s_next])[0]
                self.replay_buffer.append(QReplayBufferTuple(actions[a],
                                                             r,
                                                             next_actions))

            if i % self.optimize_every == 0:
                self.gradient_steps()

    def learn_from_experience(self):
        pass  # QLearning doesn't have a learning step at the end.

    def stats(self):
        return "replay buffer size = {}, {} solutions found".format(
            len(self.replay_buffer), self.solutions_found)

    def gradient_steps(self):
        examples = self.replay_buffer
        batch_size = min(self.batch_size, len(examples))

        if batch_size == 0:
            return

        batch = random.sample(examples, batch_size)
        ys = []

        # Compute ys.
        with torch.no_grad():
            for t in batch:
                if t.r > 0 or not t.A1:  # Next state is terminal.
                    ys.append(t.r)
                else:
                    # Need to compute maximum Q value for all actions.
                    max_q = self.q_function(t.A1).max()
                    ys.append(t.r + self.discount_factor * max_q)

        # Compute Q estimates and take gradient steps.
        self.optimizer.zero_grad()
        q_estimates = self.q_function([t.a0 for t in batch])

        y = torch.tensor(ys, dtype=q_estimates.dtype, device=q_estimates.device)
        loss = ((y - q_estimates)**2).mean()
        wandb.log({'train_loss': loss.item()})
        loss.backward()
        self.optimizer.step()


@register(LearningAgent)
class AutodidaticIteration(LearningAgent):
    def __init__(self, q_function, config):
        self.q_function = q_function

        self.batch_size = config.get('batch_size', 64)
        self.n_gradient_steps = config.get('gradient_steps', 64)
        self.reward_weight = config.get('reward_weight', 0.02)

        self.optimizer = torch.optim.Adam(q_function.parameters(),
                                          lr=config.get('learning_rate', 1e-4))
        self.examples = []

    def name(self):
        return 'AutodidaticIteration'

    def get_q_function(self):
        return self.q_function

    def learn_from_environment(self, environment):
        for i in itertools.count():
            states = [environment.generate_new() for _ in range(self.batch_size)]

            for s in states:
                r, actions = environment.step([s])[0]

                if r:
                    # Trivial state; no examples to draw.
                    continue

                with torch.no_grad():
                    q_s = self.q_function(actions)

                r_a = []

                for i, a in enumerate(actions):
                    r_a_i, _ = environment.step([a.next_state])[0]
                    r_a.append(float(r_a_i) + q_s[i].item())

                value = np.max(r_a) - self.reward_weight
                self.examples.append((s, value))

            self.gradient_steps()

    def learn_from_experience(self):
        pass

    def stats(self):
        return f"n_training_examples={len(self.examples)}"

    def gradient_steps(self):
        examples = self.examples
        batch_size = min(self.batch_size, len(examples))

        if batch_size == 0:
            return

        batch = random.sample(examples, batch_size)

        for _ in range(self.n_gradient_steps):
            y_p = self.q_function([Action(st, '', st, 0.0, 0.0) for st, _ in batch])
            y = torch.tensor([y for _, y in batch], dtype=y_p.dtype, device=y_p.device)

            self.optimizer.zero_grad()
            loss = ((y_p - y)**2).mean()
            loss.backward()
            wandb.log({'train_loss': loss.item()})
            self.optimizer.step()


@register(LearningAgent)
class DAVI(LearningAgent):
    def __init__(self, q_function, config):
        self.q_function = q_function

        self.batch_size = config.get('batch_size', 64)
        self.n_gradient_steps = config.get('gradient_steps', 64)
        self.g = config.get('step_cost', 0.01)

        self.optimizer = torch.optim.Adam(q_function.parameters(),
                                          lr=config.get('learning_rate', 1e-4))
        self.examples = []

    def name(self):
        return 'DAVI'

    def get_q_function(self):
        return self.q_function

    def learn_from_environment(self, environment):
        for i in itertools.count():
            states = [environment.generate_new() for _ in range(self.batch_size)]

            for s in states:
                r, actions = environment.step([s])[0]

                if r:
                    self.examples.append((s, 0))
                    continue

                with torch.no_grad():
                    q_s = self.q_function(actions)

                r_a = []

                for i, a in enumerate(actions):
                    r_a_i, _ = environment.step([a.next_state])[0]
                    r_a.append(self.g + (0 if r_a_i else q_s[i].item()))

                value = np.min(r_a)
                self.examples.append((s, value))

            self.gradient_steps()

    def learn_from_experience(self):
        pass

    def stats(self):
        return f"n_training_examples={len(self.examples)}"

    def gradient_steps(self):
        examples = self.examples
        batch_size = min(self.batch_size, len(examples))

        if batch_size == 0:
            return

        batch = random.sample(examples, batch_size)

        for _ in range(self.n_gradient_steps):
            y_p = self.q_function([Action(st, '', st, 0.0, 0.0) for st, _ in batch])
            y = torch.tensor([y for _, y in batch], dtype=y_p.dtype, device=y_p.device)

            self.optimizer.zero_grad()
            loss = ((y_p - y)**2).mean()
            loss.backward()
            wandb.log({'train_loss': loss.item()})
            self.optimizer.step()

@register(LearningAgent)
class BehavioralCloning(LearningAgent):
    def __init__(self, q_function, config):
        self.q_function = q_function

        self.batch_size = config.get('batch_size', 64)
        self.n_gradient_steps = config.get('gradient_steps', 5000)
        self.max_depth = config.get('max_depth', 20)

        self.optimizer = torch.optim.Adam(q_function.parameters(),
                                          lr=config.get('learning_rate', 1e-4))
        self.examples = []

    def name(self):
        return 'BehavioralCloning'

    def get_q_function(self):
        return self.q_function

    def learn_from_environment(self, environment):
        for i in itertools.count():
            s = environment.generate_new()
            r, actions = environment.step([s])[0]

            available_actions = []
            solution = []

            if r:
                # Trivial state; no examples to draw.
                continue

            for i in range(self.max_depth):
                if r or len(actions) == 0:
                    break
                available_actions.append(actions)
                a = random.randint(0, len(actions) - 1)
                solution.append(actions[a])
                r, actions = environment.step([solution[-1].next_state])[0]

            if r:
                for actions, answer in zip(available_actions, solution):
                    self.examples.append((actions, answer))

    def stats(self):
        return f"n_training_examples={len(self.examples)}"

    def learn_from_experience(self):
        examples = self.examples
        batch_size = min(self.batch_size, len(examples))

        if batch_size == 0:
            return

        celoss = nn.CrossEntropyLoss()
        losses = []

        for i in range(self.n_gradient_steps):
            (all_actions, answer) = random.choice(self.examples)
            self.optimizer.zero_grad()
            f_pred = self.q_function(all_actions)
            loss = celoss(f_pred.unsqueeze(0), answer * torch.ones(1, dtype=int, device=f_pred.device))
            # wandb.log({'train_loss': loss.item()})
            losses.append(loss.item())
            loss.backward()
            self.optimizer.step()


def run_agent_experiment(config, device):
    experiment_id = config['experiment_id']
    domain = config['domain']
    agent_name = config['agent']['name']
    run_index = config.get('run_index', 0)

    run_id = "{}-{}-{}{}".format(experiment_id, agent_name, domain, run_index)

    wandb.init(id=run_id,
               name=run_id,
               config=config,
               entity='kanishkgandhi',
               project='socratic',
               reinit=True)

    env = Environment.from_config(config)
    q_fn = QFunction.new(config['agent']['q_function'], device)
    agent = LearningAgent.new(q_fn, config['agent'])

    eval_env = EnvironmentWithEvaluationProxy(experiment_id, run_index, agent_name, domain,
                                              agent, env, config['eval_environment'])
    eval_env.evaluate_agent()


def run_batch_experiment(config, range_to_run):
    'Spawns a series of processes to run experiments for each agent/domain pair.'
    experiment_id = config.get('experiment_id', util.random_id())
    domains = config['domains']
    agents = [c for c in config['agents'] if not c.get('disable')]
    n_runs = config.get('n_runs', 1)

    environment_backend = config.get('environment_backend', 'Racket')
    environment_port_base = config.get('environment_port_base', 9876)
    port = 0

    run_processes = []
    environments = []
    agent_index = 0
    gpus = config.get('gpus', [])

    if not gpus:
        print('WARNING: no GPUs specified.')

    print('Starting experiment', experiment_id)

    try:
        for domain in domains:
            for agent in agents:
                print(f'Running {agent["name"]} on {domain}')

                for run_index in range(n_runs):
                    if agent_index < range_to_run[0] or agent_index >= range_to_run[1]:
                        print(f'Run {run_index} not in range - skipping')
                        agent_index += 1
                        continue

                    if environment_backend == 'Racket':
                        port = environment_port_base + agent_index
                        environment_process = subprocess.Popen(
                            ['racket', 'environment.rkt', '-p', str(port)],
                            stderr=subprocess.DEVNULL)
                        environments.append(environment_process)

                        # Wait for environment to be ready.
                        time.sleep(30)

                    run_config = {
                        'experiment_id': experiment_id,
                        'run_index': run_index,
                        'agent': agent,
                        'domain': domain,
                        'environment_backend': environment_backend,
                        'environment_url': 'http://localhost:{}'.format(port),
                        'multitask_train_domains': config.get('multitask_train_domains'),
                        'eval_environment': copy.deepcopy(config['eval_environment']),
                        'wandb_project': config.get('wandb_project')
                    }

                    print('Running agent with config', json.dumps(run_config))

                    agent_process = subprocess.Popen(
                        ['python3', 'agent.py', '--learn', '--config', json.dumps(run_config)]
                        + (['--gpu', str(gpus[agent_index % len(gpus)])] if gpus else []),
                        stderr=subprocess.DEVNULL)
                    run_processes.append(agent_process)

                    agent_index += 1

        print('Waiting for all agents to finish...')
        for p in run_processes:
            p.wait()
        print('Shutting down environments...')
        for p in environments:
            p.terminate()
        print('Done!')

    except (Exception, KeyboardInterrupt):
        print('Killing all created processes...')
        for p in run_processes + environments:
            p.terminate()

        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser("Train RL agents to solve symbolic domains")
    parser.add_argument('--config', help='Path to config file, or inline JSON.', required=True)
    parser.add_argument('--learn', help='Put an agent to learn from the environment', action='store_true')
    parser.add_argument('--experiment', help='Run a batch of experiments with multiple agents and environments',
                        action='store_true')
    parser.add_argument('--eval', help='Evaluate a learned policy', action='store_true')
    parser.add_argument('--eval-checkpoints', help='Show the evolution of a learned policy during interaction',
                        action='store_true')
    parser.add_argument('--debug', help='Enable debug messages.', action='store_true')
    parser.add_argument('--range', type=str, default=None,
                        help='Range of experiments to run. Format: 2-5 means range [2, 5).'
                        'Used to split experiments across multiple machines. Default: all')
    parser.add_argument('--gpu', type=int, default=None, help='Which GPU to use.')

    opt = parser.parse_args()

    try:
        if opt.config:
            config = json.loads(opt.config)
    except json.decoder.JSONDecodeError:
        config = json.load(open(opt.config))

    device = torch.device('cpu') if opt.gpu is None else torch.device(opt.gpu)

    # configure logging.
    FORMAT = '%(asctime)-15s %(message)s'
    logging.basicConfig(format=FORMAT)

    if opt.debug:
        logging.getLogger().setLevel(logging.INFO)

    if opt.range:
        range_to_run = tuple(map(int, opt.range.split('-')))
    else:
        range_to_run = (0, 10**9)

    # Only shown in debug mode.
    logging.info('Running in debug mode.')

    if opt.learn:
        run_agent_experiment(config, device)
    elif opt.eval:
        evaluate_policy(config, device)
    elif opt.eval_checkpoints:
        evaluate_policy_checkpoints(config, device)
    elif opt.experiment:
        run_batch_experiment(config, range_to_run)
