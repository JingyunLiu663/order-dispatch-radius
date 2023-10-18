import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
from config import *
from utilities import *
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime

"""
This script is used for RL to learn the optimal matching radius
DQN with replay buffer and fixed target is implemented
Attention:
    State may be stored as a customized State object in other scripts, 
    but within this RL agent construct script, State is maintained in tuple format for memory efficiency consideration
"""


class DqnNetwork(nn.Module):
    def __init__(self, input_dims: int, num_layers: int, layers_dimension_list: list, n_actions: int, lr: float):
        """
        :param input_dims: presumably 2 (time_slice, grid_id)
        :param num_layers: number of intermediate layers
        :param layers_dimension_list: a list indicating number of dimension of each layer
        :param n_actions: the action space
        :param lr: learning rate
        """
        super(DqnNetwork, self).__init__()
        self.layers = nn.ModuleList()

        # input layer
        self.layers.append(nn.Linear(input_dims, layers_dimension_list[0]))

        # middle layers
        for i in range(1, num_layers):
            self.layers.append(nn.Linear(layers_dimension_list[i - 1], layers_dimension_list[i]))

        # output layer
        self.out = nn.Linear(layers_dimension_list[-1], n_actions)

        self.optimizer = optim.Adam(self.parameters(), lr=lr)
        self.loss = nn.MSELoss()


    def forward(self, state):
        """
        Use ReLu as the activation function
        :param state: A tensor representation of tuple (time_slice, grid_id)
        """
        x = state
        for layer in self.layers:
            x = F.relu(layer(x))
        actions = self.out(x)
        return actions


class DqnAgent:
    """
        TODO: action space [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]
        The is an agent for DQN learning for dispatch problem:
        Agent: individual driver, each driver is taken as an agent, but all the agents share the same DRL parameters
        Reward: r / radius (Immediate trip fare regularized by radius)
        State: tuple (time_slice, grid_id)
        Action: matching radius applied (km)
    """

    def __init__(self, action_space: list, num_layers: int, layers_dimension_list: list, lr=0.0005, gamma=0.99,
                 epsilon=1.0, eps_min=0.01, eps_dec=0.997, target_replace_iter=2000):
        self.num_actions = len(action_space)
        self.num_layers = num_layers
        self.layers_dimension_list = layers_dimension_list
        self.input_dims = 2  # (grid_id, time_slice)
        self.lr = lr
        self.gamma = gamma
        self.epsilon = epsilon
        self.eps_min = eps_min
        self.eps_dec = eps_dec
        self.target_replace_iter = target_replace_iter  # how often do we update the target network
        self.batch_size = BATCH_SIZE

        self.eval_net_update_times = 0

        # one network as the network to be evaluated, the other as the fixed target
        self.eval_net = DqnNetwork(self.input_dims, self.num_layers, self.layers_dimension_list, self.num_actions,
                                   self.lr)
        self.target_net = DqnNetwork(self.input_dims, self.num_layers, self.layers_dimension_list, self.num_actions,
                                     self.lr)

        # to plot the loss curve
        self.loss_values = []
   
        # Create a SummaryWriter object and specify the log directory
        # current_time = datetime.now().strftime('%b%d_%H-%M-%S')
        log_dir = f"runs/experiment_dqn_{'_'.join(map(str, self.layers_dimension_list))}_{'_'.join(map(str, action_space))}"
        self.writer = SummaryWriter(log_dir)
        hparam_dict = {'lr': self.lr, 'gamma': self.gamma, 'epsilon': self.epsilon, 'eps_min': self.eps_min, 'eps_dec': self.eps_dec, 'target_replace_iter': self.target_replace_iter}
        self.writer.add_hparams(hparam_dict, {})


    def choose_action(self, states: np.array):
        """
        Choose action based on epsilon-greedy algorithm
        :param states: numpy array of shape n * 2
        :return: numpy array of action index
        """
        n = states.shape[0]
        # Convert all observations to a tensor
        state_tensor = torch.tensor(states, dtype=torch.float32)
        # Compute Q-values for all states in one forward pass
        with torch.no_grad():
            q_values = self.eval_net(state_tensor)
        # Default action selection is greedy
        action_indices = torch.argmax(q_values, dim=1).numpy()
        # Identify agents that should explore
        explorers = np.random.random(n) < self.epsilon
        # Generate random actions for explorers
        action_indices[explorers] = np.random.randint(self.num_actions, size=np.sum(explorers))

        return action_indices

    def learn(self, states, action_indices, rewards, next_states):

        # update the target network parameter
        if self.eval_net_update_times % self.target_replace_iter == 0:
            self.target_net.load_state_dict(self.eval_net.state_dict())

        # convert numpy array to tensor
        state_batch = torch.tensor(states, dtype=torch.float32)
        action_batch = torch.tensor(action_indices, dtype=torch.int64)
        reward_batch = torch.tensor(rewards, dtype=torch.float32)
        new_state_batch = torch.tensor(next_states, dtype=torch.float32)

        # RL learn by batch
        q_eval = self.eval_net(state_batch)[np.arange(BATCH_SIZE), action_batch]
        # Make sure q_eval's first dimension is always equal to batch_size, otherwise the above code will cause error

        q_next = self.target_net(new_state_batch)
        # Side notes:
        #   q_next is a 2-dimensional tensor with shape: (batch_size, num_actions)
        #   torch.max() returns a tuple:
        #     The first element ([0]) is the actual maximum values (in our case, the Q-values).
        #     The second element ([1]) is the indices of these max values.
        q_target = (reward_batch + self.gamma * torch.max(q_next, dim=1)[0]).detach()

        # calculate loss and do the back-propagation
        loss = self.eval_net.loss(q_target, q_eval)

        # to plot the loss curve
        self.loss_values.append(loss.item())
        self.writer.add_scalar('Loss', loss.item(), self.eval_net_update_times)
        self.writer.add_scalar('Reward', np.mean(rewards), self.eval_net_update_times)

        self.eval_net.optimizer.zero_grad()
        loss.backward()
        self.eval_net.optimizer.step()
        
        # Log weights and biases
        for name, param in self.eval_net.named_parameters():
            self.writer.add_histogram(name, param.clone().data.numpy(), self.eval_net_update_times)

        # update epsilon
        self.epsilon = self.epsilon - self.eps_dec if self.epsilon > self.eps_min else self.eps_min

        self.eval_net_update_times += 1

        self.writer.close()
    


    def save_parameters(self, path: str):
        """
        Save model parameters.
        
        Args:
        - path (str): The path to save the model parameters.
        """
        torch.save(self.eval_net.state_dict(), path)

    def load_parameters(self, path: str):
        """
        Load model parameters.
        
        Args:
        - path (str): The path to load the model parameters from.
        """
        self.eval_net.load_state_dict(torch.load(path))
        self.target_net.load_state_dict(torch.load(path))