import argparse
import os
# os.environ['CUDA_VISIBLE_DEVICES'] = '1'
os.environ['SDL_VIDEODRIVER'] = 'dummy'
import numpy as np
import torch
from torch import nn, optim
from torch.distributions import Normal
from torch.distributions.kl import kl_divergence
from torch.nn import functional as F
from torchvision.utils import make_grid, save_image
from tqdm import tqdm
from env_carla_obschannel import Env
from memory import ExperienceReplay
from models import bottle, Encoder, ObservationModel, RewardModel, TransitionModel, ValueModel, ActorModel
from planner import MPCPlanner
from utils import lineplot, write_video, imagine_ahead, lambda_return, FreezeParameters, ActivateParameters
from tensorboardX import SummaryWriter

os.environ['MUJOCO_GL'] = 'egl'

# Hyperparameters
parser = argparse.ArgumentParser(description='PlaNet or Dreamer')
parser.add_argument('--algo', type=str, default='dreamer', help='planet or dreamer')
parser.add_argument('--id', type=str, default='default', help='Experiment ID')
parser.add_argument('--seed', type=int, default=1, metavar='S', help='Random seed')
parser.add_argument('--disable-cuda', action='store_true', help='Disable CUDA')
parser.add_argument('--env', type=str, default='carla-v1', help='Gym/Control Suite environment')
parser.add_argument('--symbolic-env', action='store_true', help='Symbolic features')
parser.add_argument('--input_names', type=list, default=['lidar'], help='Input_channel names')  # Only one input_
parser.add_argument('--mask_names', type=list, default=[], help='Mask names')
parser.add_argument('--max-episode-length', type=int, default=2001, metavar='T', help='Max episode length')
parser.add_argument('--experience-size', type=int, default=500000, metavar='D', help='Experience replay size')  # Original implementation has an unlimited buffer size, but 1 million is the max experience collected anyway
parser.add_argument('--cnn-activation-function', type=str, default='relu', choices=dir(F), help='Model activation function for a convolution layer')
parser.add_argument('--dense-activation-function', type=str, default='elu', choices=dir(F), help='Model activation function a dense layer')
parser.add_argument('--embedding-size', type=int, default=1024, metavar='E', help='Observation embedding size')  # Note that the default encoder for visual observations outputs a 1024D vector; for other embedding sizes an additional fully-connected layer is used
parser.add_argument('--hidden-size', type=int, default=200, metavar='H', help='Hidden size')
parser.add_argument('--belief-size', type=int, default=200, metavar='H', help='Belief/hidden size')
parser.add_argument('--state-size', type=int, default=30, metavar='Z', help='State/latent size')
parser.add_argument('--action-repeat', type=int, default=1, metavar='R', help='Action repeat')
parser.add_argument('--action-noise', type=float, default=0.3, metavar='ε', help='Action noise')
parser.add_argument('--episodes', type=int, default=1500, metavar='E', help='Total number of episodes')
parser.add_argument('--seed-episodes', type=int, default=1, metavar='S', help='Seed episodes')
parser.add_argument('--collect-interval', type=int, default=100, metavar='C', help='Collect interval')
parser.add_argument('--batch-size', type=int, default=32, metavar='B', help='Batch size')
parser.add_argument('--chunk-size', type=int, default=32, metavar='L', help='Chunk size')
parser.add_argument('--worldmodel-LogProbLoss', action='store_true', help='use LogProb loss for observation_model and reward_model training')
parser.add_argument('--overshooting-distance', type=int, default=50, metavar='D', help='Latent overshooting distance/latent overshooting weight for t = 1')
parser.add_argument('--overshooting-kl-beta', type=float, default=0, metavar='β>1', help='Latent overshooting KL weight for t > 1 (0 to disable)')
parser.add_argument('--overshooting-reward-scale', type=float, default=0, metavar='R>1', help='Latent overshooting reward prediction weight for t > 1 (0 to disable)')
parser.add_argument('--global-kl-beta', type=float, default=0, metavar='βg', help='Global KL weight (0 to disable)')
parser.add_argument('--free-nats', type=float, default=3, metavar='F', help='Free nats')
parser.add_argument('--bit-depth', type=int, default=5, metavar='B', help='Image bit depth (quantisation)')
parser.add_argument('--model_learning-rate', type=float, default=1e-3, metavar='α', help='Learning rate') 
parser.add_argument('--actor_learning-rate', type=float, default=8e-5, metavar='α', help='Learning rate') 
parser.add_argument('--value_learning-rate', type=float, default=8e-5, metavar='α', help='Learning rate') 
parser.add_argument('--learning-rate-schedule', type=int, default=0, metavar='αS', help='Linear learning rate schedule (optimisation steps from 0 to final learning rate; 0 to disable)') 
parser.add_argument('--adam-epsilon', type=float, default=1e-7, metavar='ε', help='Adam optimizer epsilon value') 
# Note that original has a linear learning rate decay, but it seems unlikely that this makes a significant difference
parser.add_argument('--grad-clip-norm', type=float, default=100.0, metavar='C', help='Gradient clipping norm')
parser.add_argument('--planning-horizon', type=int, default=15, metavar='H', help='Planning horizon distance')
parser.add_argument('--discount', type=float, default=0.99, metavar='H', help='Planning horizon distance')
parser.add_argument('--disclam', type=float, default=0.95, metavar='H', help='discount rate to compute return')
parser.add_argument('--optimisation-iters', type=int, default=10, metavar='I', help='Planning optimisation iterations')
parser.add_argument('--candidates', type=int, default=1000, metavar='J', help='Candidate samples per iteration')
parser.add_argument('--top-candidates', type=int, default=100, metavar='K', help='Number of top candidates to fit')
parser.add_argument('--test', action='store_true', help='Test only')
parser.add_argument('--test-interval', type=int, default=10, metavar='I', help='Test interval (episodes)')
parser.add_argument('--test-episodes', type=int, default=1, metavar='E', help='Number of test episodes')
parser.add_argument('--checkpoint-interval', type=int, default=50, metavar='I', help='Checkpoint interval (episodes)')
parser.add_argument('--checkpoint-experience', default=False, help='Checkpoint experience replay')
parser.add_argument('--models', type=str, default='', metavar='M', help='Load model checkpoint')
parser.add_argument('--experience-replay', type=str, default='', metavar='ER', help='Load experience replay')
parser.add_argument('--render', action='store_true', help='Render environment')
parser.add_argument('--expgroup', type=str, default='')
parser.add_argument('--imaginetraj', type=int, default=1)
args = parser.parse_args()
args.overshooting_distance = min(args.chunk_size, args.overshooting_distance)  # Overshooting distance cannot be greater than chunk size
print(' ' * 26 + 'Options')
for k, v in vars(args).items():
  print(' ' * 26 + k + ': ' + str(v))


# Setup
results_dir = os.path.join('results-{}'.format(args.expgroup), '{}_{}'.format(args.env, args.id))
os.makedirs(results_dir, exist_ok=True)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
if torch.cuda.is_available() and not args.disable_cuda:
  print("using CUDA")
  args.device = torch.device('cuda')
  torch.cuda.manual_seed(args.seed)
  #solve a pytorch bug : unable to find a vaild cudnn algorithm to run convolution
  torch.backends.cudnn.benchmark = True
  torch.backends.cudnn.enabled = False
else:
  print("using CPU")
  args.device = torch.device('cpu')
global input_names
input_names = args.input_names
print('input_names',input_names)
metrics = {'steps': [], 'episodes': [], 'train_rewards': [], 'test_episodes': [], 'test_rewards': [], 
           'reward_loss': [], 'kl_loss': [], 'actor_loss': [], 'value_loss': [], 'test_episode_length':[], 'test_collision_distance':[], 'test_collision_times':[],
           'test_collision_percentage': [], 'model_loss': [], 'action_loss': [], 'bc_weight': [], 'replay_buffer_rewards':[], 'replay_buffer_length':[], 'replay_buffer_collision_distance':[], }
for name in input_names:
  metrics.update({'observation_loss'+name: []})
summary_name = results_dir + "/{}_{}_log"
writer = SummaryWriter(summary_name.format(args.env, args.id))

# Initialise training environment and experience replay memory
env = Env(args.env, args.symbolic_env, args.seed, args.max_episode_length, args.action_repeat, args.bit_depth, args.input_names+args.mask_names)
if args.experience_replay is not '' and os.path.exists(args.experience_replay):
  D = torch.load(args.experience_replay)
  metrics['steps'], metrics['episodes'] = [D.steps] * D.episodes, list(range(1, D.episodes + 1))
elif not args.test:
  D = ExperienceReplay(args.experience_size, args.symbolic_env, env.observation_size, env.action_size, args.bit_depth, args.device, args.input_names)
  # Initialise dataset D with S random seed episodes
  for s in range(1, args.seed_episodes + 1):
    observation, done, t = env.reset(), False, 0
    while not done:
      action = env.sample_random_action()
      next_observation, reward, done, _,_= env.step(action)
      D.append(observation, action, reward, done)
      observation = next_observation
      t += 1
    metrics['steps'].append(t * args.action_repeat + (0 if len(metrics['steps']) == 0 else metrics['steps'][-1]))
    metrics['episodes'].append(s)
    print('steps', metrics['steps'])


# Initialise model parameters randomly
transition_model = TransitionModel(args.belief_size, args.state_size, env.action_size, args.hidden_size, args.embedding_size, args.dense_activation_function).to(device=args.device)
observation_model = ObservationModel(args.symbolic_env, env.observation_size, args.belief_size, args.state_size, args.embedding_size, args.cnn_activation_function,args.input_names, args.mask_names).to(device=args.device)
reward_model = RewardModel(args.belief_size, args.state_size, args.hidden_size, args.dense_activation_function).to(device=args.device)
encoder = Encoder(args.symbolic_env, env.observation_size, args.embedding_size,args.cnn_activation_function, args.input_names, args.device).to(device=args.device)
actor_model = ActorModel(args.belief_size, args.state_size, args.hidden_size, env.action_size, args.dense_activation_function).to(device=args.device)
value_model = ValueModel(args.belief_size, args.state_size, args.hidden_size, args.dense_activation_function).to(device=args.device)
param_list = list(transition_model.parameters()) + list(observation_model.parameters()) + list(reward_model.parameters()) + list(encoder.parameters())
value_actor_param_list = list(value_model.parameters()) + list(actor_model.parameters())
params_list = param_list + value_actor_param_list
model_optimizer = optim.Adam(param_list, lr=0 if args.learning_rate_schedule != 0 else args.model_learning_rate, eps=args.adam_epsilon)
actor_optimizer = optim.Adam(actor_model.parameters(), lr=0 if args.learning_rate_schedule != 0 else args.actor_learning_rate, eps=args.adam_epsilon)
value_optimizer = optim.Adam(value_model.parameters(), lr=0 if args.learning_rate_schedule != 0 else args.value_learning_rate, eps=args.adam_epsilon)
if args.models is not '' and os.path.exists(args.models):
  model_dicts = torch.load(args.models)
  transition_model.load_state_dict(model_dicts['transition_model'])
  observation_model.load_state_dict(model_dicts['observation_model'])
  reward_model.load_state_dict(model_dicts['reward_model'])
  encoder.load_state_dict(model_dicts['encoder'])
  actor_model.load_state_dict(model_dicts['actor_model'])
  value_model.load_state_dict(model_dicts['value_model'])
  model_optimizer.load_state_dict(model_dicts['model_optimizer'])
if args.algo=="dreamer":
  print("DREAMER")
  planner = actor_model
else:
  planner = MPCPlanner(env.action_size, args.planning_horizon, args.optimisation_iters, args.candidates, args.top_candidates, transition_model, reward_model)
global_prior = Normal(torch.zeros(args.batch_size, args.state_size, device=args.device), torch.ones(args.batch_size, args.state_size, device=args.device))  # Global prior N(0, I)
free_nats = torch.full((1, ), args.free_nats, device=args.device)  # Allowed deviation in KL divergence


def update_belief_and_act(args, env, planner, transition_model, encoder, belief, posterior_state, action, observation, explore=False):
  # Infer belief over current state q(s_t|o≤t,a<t) from the history
  belief, _, _, _, posterior_state, _ ,_, = transition_model(posterior_state, action.unsqueeze(dim=0), belief, encoder(observation).unsqueeze(dim=0))  # Action and observation need extra time dimension
  belief, posterior_state = belief.squeeze(dim=0), posterior_state.squeeze(dim=0)  # Remove time dimension from belief/state
  if args.algo=="dreamer":
    action = planner.get_action(belief, posterior_state, det=not(explore))
  else:
    action = planner(belief, posterior_state)  # Get action from planner(q(s_t|o≤t,a<t), p)
  if explore:
    action = torch.clamp(Normal(action, args.action_noise).rsample(), -1, 1) # Add gaussian exploration noise on top of the sampled action
    # action = action + args.action_noise * torch.randn_like(action)  # Add exploration noise ε ~ p(ε) to the action
  action = action.squeeze(dim=0)
  action [0] = action[0]
  action [1] = action[1]
  action1= torch.tensor([action[0], action[1]]).unsqueeze(dim=0)
  action2 = torch.tensor([action[0], action[1]])
  next_observation, reward, done, collision_times, vehicle_collision_distance = env.step(action2,training=True)

  return belief, posterior_state, action1, next_observation, reward, done, collision_times, vehicle_collision_distance



# Testing only
if args.test:
  print("OnlyEvaluate model")
  test_envs = env
  avg_dis = []
  D = ExperienceReplay(args.experience_size, args.symbolic_env, env.observation_size, env.action_size, args.bit_depth,
                       args.device, args.input_names + args.mask_names)
  for episode in range(0, 1):
    # Set models to eval mode
    transition_model.eval()
    observation_model.eval()
    reward_model.eval()
    encoder.eval()
    actor_model.eval()
    value_model.eval()
    bc_model.eval()
    # Initialise parallelised test environments
    with torch.no_grad():
      observation = test_envs.reset()
      total_rewards, video_frames = np.zeros((args.test_episodes,)), []
      belief, posterior_state, action = torch.zeros(args.test_episodes, args.belief_size,device=args.device), torch.zeros(args.test_episodes, args.state_size,device=args.device), torch.zeros(args.test_episodes, env.action_size, device=args.device)
      pbar = tqdm(range(args.max_episode_length // args.action_repeat))
      test_episode_length = 0
      test_collision_distance = 0
      test_collision_times = 0
      vehicle_work_distance = 0
      for t in pbar:
        belief, posterior_state, action, next_observation, reward, done, collision_time, vehicle_collision_distance = update_belief_and_act(args, test_envs,planner,transition_model,encoder, belief,posterior_state,action.to( device=args.device), observation)
        total_rewards += reward
        if not args.symbolic_env:
          # Collect real vs. predicted frames for video
          belief_posterior = observation_model(belief, posterior_state)
          multi_posterior_observation = torch.cat(list(belief_posterior.values()), dim=-1).cpu()
          multi_observation = torch.cat(list(observation.values()), dim=-1).cpu()
          video_frames.append(make_grid(torch.cat([multi_observation, multi_posterior_observation], dim=-2) + 0.5, nrow=5).numpy())  # Decentre

        observation = next_observation
        test_collision_times += collision_time
        test_episode_length += 1
        vehicle_work_distance += vehicle_collision_distance

        if args.render:
          test_envs.render()
        if done:
          pbar.close()
          break
        D.append(observation, action, reward, done)
      torch.save(D, os.path.join(results_dir, 'D2.pth'))
      avg_dis.append(vehicle_work_distance)



    # Update and plot reward metrics (and write video if applicable) and save metrics
    test_collision_percentage = test_collision_times / vehicle_work_distance
    metrics['test_episode_length'].append(test_episode_length)
    metrics['test_collision_percentage'].append(test_collision_percentage)
    metrics['test_collision_distance'].append(vehicle_work_distance)
    print('avg_dis list', avg_dis)
    print('Avg dis', np.mean(avg_dis))
    print('Max dis', np.max(avg_dis))
    print('Collison rate', np.mean(metrics['test_collision_percentage']))
    metrics['test_episodes'].append(episode)
    lineplot(metrics['test_episode_length'][-len(metrics['test_episode_length']):], metrics['test_episode_length'], 'OnlyEvaluate_episode_length', results_dir)
    lineplot(metrics['test_collision_distance'][-len(metrics['test_collision_distance']):], metrics['test_collision_distance'], 'OnlyEvaluate_episode_collision_distance', results_dir)
    lineplot(metrics['test_collision_percentage'][-len(metrics['test_collision_percentage']):], metrics['test_collision_percentage'], 'OnlyEvaluate_episode_collision_percentage', results_dir)
    if not args.symbolic_env:
      episode_str = str(episode).zfill(len(str(args.episodes)))
      write_video(video_frames, 'onlytest_episode_%s' % episode_str, results_dir)  # Lossy compression
      save_image(torch.as_tensor(video_frames[-1]), os.path.join(results_dir, 'onlytest_episode_%s.png' % episode_str))
      video = np.multiply(np.stack(video_frames, axis=0), 255).clip(0, 255).astype(np.uint8)
      video = torch.as_tensor(video).unsqueeze(dim=0)
      writer.add_video('OnlyEpisode_Observation/Reconstruction_Video', video, global_step=metrics['test_episodes'][-1] * args.action_repeat, fps=4)
    torch.save(metrics, os.path.join(results_dir, 'metrics.pth'))

    # Set models to train mode
    transition_model.train()
    observation_model.train()
    reward_model.train()
    encoder.train()
    actor_model.train()
    value_model.train()
    # Close test environments
    test_envs.close()

    writer.add_scalar("OnlyEvaluate/episode_length", np.mean(metrics['test_episode_length'][-1]), metrics['test_episodes'][-1])
    writer.add_scalar("OnlyEvaluate/episode_collision_distance", np.mean(metrics['test_collision_distance'][-1]), metrics['test_episodes'][-1])
    writer.add_scalar("OnlyEvaluate/episode_collision_percentage", np.mean(metrics['test_collision_percentage'][-1]), metrics['test_episodes'][-1])
  
  env.close()
  quit()

# from IPython import embed; embed()
# exit(1)
average_reward_metrics = []
average_length_metrics = []
average_collision_percentage = []
average_collision_distance = []
episode_length = []
Average_Return = {'AverageReturn_Rewards': [], 'AverageReturn_Length': [], 'AverageCollision_percentage': [] ,'AverageCollision_distance': []}

# Training (and testing)
for episode in tqdm(range(metrics['episodes'][-1] + 1, args.episodes + 1), total=args.episodes, initial=metrics['episodes'][-1] + 1):
  # Model fitting
  losses = []
  model_modules = transition_model.modules+encoder.modules+observation_model.modules+reward_model.modules

  print("training loop")
  for s in tqdm(range(args.collect_interval)):
    # Draw sequence chunks {(o_t, a_t, r_t+1, terminal_t+1)} ~ D uniformly at random from the dataset (including terminal flags)
    observations, actions, rewards, nonterminals = D.sample(args.batch_size, args.chunk_size) # Transitions start at time t = 0
    # Create initial belief and state for time t = 0
    init_belief, init_state = torch.zeros(args.batch_size, args.belief_size, device=args.device), torch.zeros(args.batch_size, args.state_size, device=args.device)
    # Update belief/state using posterior from previous belief/state, previous action and current observation (over entire sequence at once)
    beliefs, prior_states, prior_means, prior_std_devs, posterior_states, posterior_means, posterior_std_devs = transition_model(init_state, actions[:-1], init_belief, bottle(encoder, observations, args.input_names), nonterminals[:-1])
    # Calculate observation likelihood, reward likelihood and KL losses (for t = 0 only for latent overshooting); sum over final dims, average over batch and time (original implementation, though paper seems to miss 1/T scaling?)
    observation_loss = {}
    observation_dist = {}
    for name in args.input_names + args.mask_names:
      if args.worldmodel_LogProbLoss:
        belief_posterior_states = bottle(observation_model, (beliefs, posterior_states), args.input_names,args.mask_names)
        observation_dist[name] = Normal(belief_posterior_states[name], 1)
        observation_loss[name] = -observation_dist[name].log_prob(observations[name][1:]).sum(dim=2 if args.symbolic_env else (2, 3, 4)).mean(dim=(0, 1))
      else:
        belief_posterior_states = bottle(observation_model, (beliefs, posterior_states), args.input_names,args.mask_names)
        observation_loss[name] = F.mse_loss(belief_posterior_states[name], observations[name][1:],reduction='none').sum(dim=2 if args.symbolic_env else (2, 3, 4)).mean(dim=(0, 1))
    if args.worldmodel_LogProbLoss:
      reward_dist = Normal(bottle(reward_model, (beliefs, posterior_states)),1)
      reward_loss = -reward_dist.log_prob(rewards[:-1]).mean(dim=(0, 1))
    else:
      reward_loss = F.mse_loss(bottle(reward_model, (beliefs, posterior_states)), rewards[:-1], reduction='none').mean(dim=(0,1))
    # transition loss
    div = kl_divergence(Normal(posterior_means, posterior_std_devs), Normal(prior_means, prior_std_devs)).sum(dim=2)
    kl_loss = torch.max(div, free_nats).mean(dim=(0, 1))  # Note that normalisation by overshooting distance and weighting by overshooting distance cancel out
    if args.global_kl_beta != 0:
      kl_loss += args.global_kl_beta * kl_divergence(Normal(posterior_means, posterior_std_devs), global_prior).sum(dim=2).mean(dim=(0, 1))
    # Calculate latent overshooting objective for t > 0
    if args.overshooting_kl_beta != 0:
      overshooting_vars = []  # Collect variables for overshooting to process in batch
      for t in range(1, args.chunk_size - 1):
        d = min(t + args.overshooting_distance, args.chunk_size - 1)  # Overshooting distance
        t_, d_ = t - 1, d - 1  # Use t_ and d_ to deal with different time indexing for latent states
        seq_pad = (0, 0, 0, 0, 0, t - d + args.overshooting_distance)  # Calculate sequence padding so overshooting terms can be calculated in one batch
        # Store (0) actions, (1) nonterminals, (2) rewards, (3) beliefs, (4) prior states, (5) posterior means, (6) posterior standard deviations and (7) sequence masks
        overshooting_vars.append((F.pad(actions[t:d], seq_pad), F.pad(nonterminals[t:d], seq_pad), F.pad(rewards[t:d], seq_pad[2:]), beliefs[t_], prior_states[t_], F.pad(posterior_means[t_ + 1:d_ + 1].detach(), seq_pad), F.pad(posterior_std_devs[t_ + 1:d_ + 1].detach(), seq_pad, value=1), F.pad(torch.ones(d - t, args.batch_size, args.state_size, device=args.device), seq_pad)))  # Posterior standard deviations must be padded with > 0 to prevent infinite KL divergences
      overshooting_vars = tuple(zip(*overshooting_vars))
      # Update belief/state using prior from previous belief/state and previous action (over entire sequence at once)
      beliefs, prior_states, prior_means, prior_std_devs = transition_model(torch.cat(overshooting_vars[4], dim=0), torch.cat(overshooting_vars[0], dim=1), torch.cat(overshooting_vars[3], dim=0), None, torch.cat(overshooting_vars[1], dim=1))
      seq_mask = torch.cat(overshooting_vars[7], dim=1)
      # Calculate overshooting KL loss with sequence mask
      kl_loss += (1 / args.overshooting_distance) * args.overshooting_kl_beta * torch.max((kl_divergence(Normal(torch.cat(overshooting_vars[5], dim=1), torch.cat(overshooting_vars[6], dim=1)), Normal(prior_means, prior_std_devs)) * seq_mask).sum(dim=2), free_nats).mean(dim=(0, 1)) * (args.chunk_size - 1)  # Update KL loss (compensating for extra average over each overshooting/open loop sequence) 
      # Calculate overshooting reward prediction loss with sequence mask
      if args.overshooting_reward_scale != 0: 
        reward_loss += (1 / args.overshooting_distance) * args.overshooting_reward_scale * F.mse_loss(bottle(reward_model, (beliefs, prior_states)) * seq_mask[:, :, 0], torch.cat(overshooting_vars[2], dim=1), reduction='none').mean(dim=(0, 1)) * (args.chunk_size - 1)  # Update reward loss (compensating for extra average over each overshooting/open loop sequence) 
    # Apply linearly ramping learning rate schedule
    if args.learning_rate_schedule != 0:
      for group in model_optimizer.param_groups:
        group['lr'] = min(group['lr'] + args.model_learning_rate / args.model_learning_rate_schedule, args.model_learning_rate)
    model_loss = sum(observation_loss.values()) + reward_loss + kl_loss
    # Update model parameters
    model_optimizer.zero_grad()
    model_loss.backward()
    nn.utils.clip_grad_norm_(param_list, args.grad_clip_norm, norm_type=2)
    model_optimizer.step()

    #Dreamer implementation: actor loss calculation and optimization    
    with torch.no_grad():
      actor_states = posterior_states.detach()
      actor_beliefs = beliefs.detach()

    imged_beliefs, imged_prior_states, imged_prior_means, imged_prior_std_devs = [], [], [], []
    for img_t in range(args.imaginetraj):
      with FreezeParameters(model_modules):
        imagination_traj = imagine_ahead(actor_states, actor_beliefs, actor_model, transition_model, args.planning_horizon)
      imged_beliefs_, imged_prior_states_, imged_prior_means_, imged_prior_std_devs_ = imagination_traj
      imged_beliefs.append(imged_beliefs_)
      imged_prior_states.append(imged_prior_states_)
      imged_prior_means.append(imged_prior_means_)
      imged_prior_std_devs.append(imged_prior_std_devs_)
    
    imged_beliefs = torch.stack(imged_beliefs, dim=0)
    imged_beliefs = imged_beliefs.view((-1,) + imged_beliefs.shape[2:])
    imged_prior_states = torch.stack(imged_prior_states, dim=0)
    imged_prior_states = imged_prior_states.view((-1,) + imged_prior_states.shape[2:])
    imged_prior_means = torch.stack(imged_prior_means, dim=0)
    imged_prior_means = imged_prior_means.view((-1,) + imged_prior_means.shape[2:])
    imged_prior_std_devs = torch.stack(imged_prior_std_devs, dim=0)
    imged_prior_std_devs = imged_prior_std_devs.view((-1,) + imged_prior_std_devs.shape[2:])

    with FreezeParameters(model_modules + value_model.modules):
      imged_reward = bottle(reward_model, (imged_beliefs, imged_prior_states))
      value_pred = bottle(value_model, (imged_beliefs, imged_prior_states))
    returns = lambda_return(imged_reward, value_pred, bootstrap=value_pred[-1], discount=args.discount, lambda_=args.disclam)
    actor_loss = -torch.mean(returns)
    # Update model parameters
    actor_optimizer.zero_grad()
    actor_loss.backward()
    nn.utils.clip_grad_norm_(actor_model.parameters(), args.grad_clip_norm, norm_type=2)
    actor_optimizer.step()
 
    #Dreamer implementation: value loss calculation and optimization
    with torch.no_grad():
      value_beliefs = imged_beliefs.detach()
      value_prior_states = imged_prior_states.detach()
      target_return = returns.detach()
    value_dist = Normal(bottle(value_model, (value_beliefs, value_prior_states)),1) # detach the input tensor from the transition network.
    value_loss = -value_dist.log_prob(target_return).mean(dim=(0, 1)) 
    # Update model parameters
    value_optimizer.zero_grad()
    value_loss.backward()
    nn.utils.clip_grad_norm_(value_model.parameters(), args.grad_clip_norm, norm_type=2)
    value_optimizer.step()
    
    # # Store (0) observation loss (1) reward loss (2) KL loss (3) actor loss (4) value loss
    losses.append([reward_loss.item(), kl_loss.item(), actor_loss.item(), value_loss.item()])
    for name in input_names:
      losses[0].append([observation_loss[name].item()])


  # Update and plot loss metrics
  losses = tuple(zip(*losses))
  metrics['reward_loss'].append(losses[0])
  metrics['kl_loss'].append(losses[1])
  metrics['actor_loss'].append(losses[2])
  metrics['value_loss'].append(losses[3])
  for name in input_names:
    metrics['observation_loss' + name].append(losses[-1])
    lineplot(metrics['episodes'][-len(metrics['observation_loss' + name]):], metrics['observation_loss' + name],'observation_loss' + name, results_dir)
  lineplot(metrics['episodes'][-len(metrics['reward_loss']):], metrics['reward_loss'], 'reward_loss', results_dir)
  lineplot(metrics['episodes'][-len(metrics['kl_loss']):], metrics['kl_loss'], 'kl_loss', results_dir)
  lineplot(metrics['episodes'][-len(metrics['actor_loss']):], metrics['actor_loss'], 'actor_loss', results_dir)
  lineplot(metrics['episodes'][-len(metrics['value_loss']):], metrics['value_loss'], 'value_loss', results_dir)


  # Data collection
  print("Data collection")
  with torch.no_grad():
    observation, total_reward = env.reset(), 0
    belief, posterior_state, action = torch.zeros(1, args.belief_size, device=args.device), torch.zeros(1,args.state_size,device=args.device), torch.zeros(1, env.action_size, device=args.device)
    pbar = tqdm(range(args.max_episode_length // 4*args.action_repeat))
    run_length = 0
    collision_times = 0
    average_return_length = 0
    vehicle_work_distance = 0
    for t in pbar:
      # print("step",t)
      run_length += 1
      belief, posterior_state, action, next_observation, reward, done, collision_time, vehicle_collision_distance = update_belief_and_act(args, env,planner,transition_model,encoder,belief,posterior_state,action.to(device=args.device),observation,explore=False)  # dreamer explore = True
      action = action.squeeze(dim=0)
      collision_times += collision_time
      # D.append(observation, action.cpu(), reward, done)
      D.append(observation, action, reward, done)
      action = action.unsqueeze(dim=0)
      total_reward += reward
      observation = next_observation
      vehicle_work_distance += vehicle_collision_distance
      if args.render:
        env.render()
      if done:
        pbar.close()
        break
    print('D_each_collect_steps', metrics['steps'])
    collision_percentage = collision_times / vehicle_work_distance
    print('train_total_reward', total_reward)
    average_return_length += run_length * args.action_repeat
    average_return_reward = 0
    average_return_reward += total_reward
    average_reward_metrics.append(average_return_reward)
    average_length_metrics.append(average_return_length)
    average_collision_percentage.append(collision_percentage)
    average_collision_distance.append(vehicle_work_distance)

    episode_length.append(episode)
    if len(episode_length) == 10:
      Average_return_reward = np.mean(average_reward_metrics)
      Average_return_length = np.mean(average_length_metrics)
      AverageCollison_distance = np.mean(average_collision_distance)
      AverageCollison_percentage = np.mean(average_collision_percentage)
      Average_Return['AverageReturn_Length'].append(Average_return_length)
      Average_Return['AverageReturn_Rewards'].append(Average_return_reward)
      Average_Return['AverageCollision_distance'].append(AverageCollison_distance)
      Average_Return['AverageCollision_percentage'].append(AverageCollison_percentage)
      episode_length, average_reward_metrics, average_length_metrics, average_collision_percentage, average_collision_distance = [], [], [], [], []
      writer.add_scalar('Collision_PercentageStep', Average_Return['AverageCollision_percentage'][-1],metrics['steps'][-1] * args.action_repeat)
      writer.add_scalar('Collision_DistanceStep', Average_Return['AverageCollision_distance'][-1],metrics['steps'][-1] * args.action_repeat)
      writer.add_scalar("AverageReturn_RewardsStep", Average_Return['AverageReturn_Rewards'][-1],metrics['steps'][-1] * args.action_repeat)
      writer.add_scalar("AverageReturn_LengthStep", Average_Return['AverageReturn_Length'][-1],metrics['steps'][-1] * args.action_repeat)
      writer.add_scalar('Collision_Percentage', Average_Return['AverageCollision_percentage'][-1],metrics['episodes'][-1])
      writer.add_scalar('Collision_Distance', Average_Return['AverageCollision_distance'][-1], metrics['episodes'][-1])
      writer.add_scalar("AverageReturn_Rewards", Average_Return['AverageReturn_Rewards'][-1], metrics['episodes'][-1])
      writer.add_scalar("AverageReturn_Length", Average_Return['AverageReturn_Length'][-1], metrics['episodes'][-1])

    # Update and plot train reward metrics
    metrics['steps'].append(t + metrics['steps'][-1])
    metrics['episodes'].append(episode)
    metrics['train_rewards'].append(total_reward)
    lineplot(metrics['episodes'][-len(metrics['train_rewards']):], metrics['train_rewards'], 'train_rewards',
             results_dir)

  # Test model
  print("Test model")
  if episode % args.test_interval == 0:
    # Set models to eval mode
    transition_model.eval()
    observation_model.eval()
    reward_model.eval()
    encoder.eval()
    actor_model.eval()
    value_model.eval()
    # Initialise parallelised test environments
    # test_envs = EnvBatcher(Env, (args.env, args.symbolic_env, args.seed, args.max_episode_length, args.action_repeat, args.bit_depth), {}, args.test_episodes)
    test_envs = env
    with torch.no_grad():
      observation, total_rewards, video_frames = test_envs.reset(), np.zeros((args.test_episodes,)), []
      belief, posterior_state, action = torch.zeros(args.test_episodes, args.belief_size,device=args.device), torch.zeros(args.test_episodes,args.state_size,device=args.device), torch.zeros(
        args.test_episodes, env.action_size, device=args.device)
      pbar = tqdm(range(args.max_episode_length // args.action_repeat))
      test_episode_length = 0
      test_collision_distance = 0
      test_collision_times = 0
      vehicle_work_distance = 0
      for t in pbar:
        belief, posterior_state, action, next_observation, reward, done, collision_time, vehicle_collision_distance = update_belief_and_act(args,test_envs,planner,transition_model,encoder,belief,posterior_state,action.to(device=args.device),observation)
        total_rewards += reward
        if not args.symbolic_env:
          # Collect real vs. predicted frames for video
          belief_posterior = observation_model(belief, posterior_state)
          multi_posterior_observation = torch.cat(list(belief_posterior.values()), dim=-1).cpu()
          multi_observation = torch.cat(list(observation.values()), dim=-1).cpu()
          video_frames.append(make_grid(torch.cat([multi_observation, multi_posterior_observation], dim=-2) + 0.5,nrow=5).numpy())  # Decentre

        observation = next_observation
        test_collision_times += collision_time
        test_episode_length += 1
        vehicle_work_distance += vehicle_collision_distance

        if args.render:
          test_envs.render()
        if done:
          pbar.close()
          break
    # Update and plot reward metrics (and write video if applicable) and save metrics
    test_collision_percentage = test_collision_times / vehicle_work_distance
    metrics['test_episode_length'].append(test_episode_length)
    metrics['test_collision_percentage'].append(test_collision_percentage)
    metrics['test_collision_distance'].append(vehicle_work_distance)
    metrics['test_episodes'].append(episode)
    metrics['test_rewards'].append(total_rewards.tolist())
    lineplot(metrics['test_episodes'], metrics['test_rewards'], 'test_rewards', results_dir)
    lineplot(np.asarray(metrics['steps'])[np.asarray(metrics['test_episodes']) - 1], metrics['test_rewards'], 'Evaluate_rewards_steps', results_dir, xaxis='step')
    lineplot(metrics['test_episode_length'][-len(metrics['test_episode_length']):], metrics['test_episode_length'], 'Evaluate_episode_length', results_dir)
    lineplot(metrics['test_collision_distance'][-len(metrics['test_collision_distance']):], metrics['test_collision_distance'], 'Evaluate_episode_collision_distance', results_dir)
    lineplot(metrics['test_collision_percentage'][-len(metrics['test_collision_percentage']):], metrics['test_collision_percentage'], 'Evaluate_episode_collision_percentage', results_dir)
    if args.symbolic_env:
      episode_str = str(episode).zfill(len(str(args.episodes)))
      write_video(video_frames, 'test_episode_%s' % episode_str, results_dir)  # Lossy compression
      save_image(torch.as_tensor(video_frames[-1]), os.path.join(results_dir, 'test_episode_%s.png' % episode_str))
      video = np.multiply(np.stack(video_frames, axis=0), 255).clip(0, 255).astype(np.uint8)
      video = torch.as_tensor(video).unsqueeze(dim=0)
      writer.add_video('StepObservation/Reconstruction_Video', video, global_step=metrics['steps'][-1] * args.action_repeat,fps=4)
      writer.add_video('EpisodeObservation/Reconstruction_Video', video,global_step=metrics['episodes'][-1] * args.action_repeat, fps=4)
    torch.save(metrics, os.path.join(results_dir, 'metrics.pth'))
    # Set models to train mode
    transition_model.train()
    observation_model.train()
    reward_model.train()
    encoder.train()
    actor_model.train()
    value_model.train()
    # Close test environments
    test_envs.close()

    writer.add_scalar("Evaluate/test_reward", np.mean(metrics['test_rewards'][-1]), metrics['steps'][-1]*args.action_repeat)
    writer.add_scalar("Evaluate/episode_reward", np.mean(metrics['test_rewards'][-1]), metrics['test_episodes'][-1])

    writer.add_scalar("Evaluate_episode_length", np.mean(metrics['test_episode_length'][-1]),metrics['steps'][-1] * args.action_repeat)
    writer.add_scalar("Evaluate/episode_length", np.mean(metrics['test_episode_length'][-1]), metrics['test_episodes'][-1])

    writer.add_scalar("Evaluate/collision_distance", np.mean(metrics['test_collision_distance'][-1]),metrics['steps'][-1] * args.action_repeat)
    writer.add_scalar("Evaluate/episode_collision_distance", np.mean(metrics['test_collision_distance'][-1]), metrics['test_episodes'][-1])

    writer.add_scalar("Evaluate/collision_percentage", np.mean(metrics['test_collision_percentage'][-1]), metrics['steps'][-1] * args.action_repeat)
    writer.add_scalar("Evaluate/episode_collision_percentage", np.mean(metrics['test_collision_percentage'][-1]), metrics['test_episodes'][-1])


  writer.add_scalar("train/train_reward", metrics['train_rewards'][-1], metrics['steps'][-1])
  writer.add_scalar("train/env_step_reward", metrics['train_rewards'][-1], metrics['steps'][-1] * args.action_repeat)
  writer.add_scalar("train/episode_reward", metrics['train_rewards'][-1], metrics['episodes'][-1])
  for name in input_names:
    writer.add_scalar("observation_loss" + name, metrics['observation_loss' + name][-1][-1], metrics['steps'][-1])
    writer.add_scalar("EpisodeObservation_loss" + name, metrics['observation_loss' + name][-1][-1], metrics['episodes'][-1])
  writer.add_scalar("reward_loss", metrics['reward_loss'][-1][-1], metrics['steps'][-1])
  writer.add_scalar("kl_loss", metrics['kl_loss'][-1][-1], metrics['steps'][-1])
  writer.add_scalar("actor_loss", metrics['actor_loss'][-1][-1], metrics['steps'][-1])
  writer.add_scalar("value_loss", metrics['value_loss'][-1][-1], metrics['steps'][-1])
  print("episodes: {}, total_steps: {}, train_reward: {} ".format(metrics['episodes'][-1], metrics['steps'][-1],metrics['train_rewards'][-1]))

  # Checkpoint models
  if episode % args.checkpoint_interval == 0:
    torch.save({'transition_model': transition_model.state_dict(),
                'observation_model': observation_model.state_dict(),
                'reward_model': reward_model.state_dict(),
                'encoder': encoder.state_dict(),
                'actor_model': actor_model.state_dict(),
                'value_model': value_model.state_dict(),
                'model_optimizer': model_optimizer.state_dict(),
                'actor_optimizer': actor_optimizer.state_dict(),
                'value_optimizer': value_optimizer.state_dict()
                }, os.path.join(results_dir, 'models_%d.pth' % episode))
    if args.checkpoint_experience:
      torch.save(D, os.path.join(results_dir, 'experience.pth'))  # Warning: will fail with MemoryError with large memory sizes


# Close training environment
env.close()