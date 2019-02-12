import os
import gc
import random
import pprint
from six.moves import range
from markdown2 import markdown
from time import gmtime, strftime
from timeit import default_timer as timer

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable

import options
from dataloader import VisDialDataset
from torch.utils.data import DataLoader
from eval_utils.rank_answerer import rankABot
from eval_utils.rank_questioner import rankQBot
from utils import utilities as utils
from utils.visualize import VisdomVisualize

try:
    import nsml
    from nsml import IS_ON_NSML
    print('able to use nsml')
except ImportError:
    pass

#---------------------------------------------------------------------------------------
# Setup
#---------------------------------------------------------------------------------------

# Read the command line options
params = options.readCommandLine()

# Seed rng for reproducibility
random.seed(params['randomSeed'])
torch.manual_seed(params['randomSeed'])
if params['useGPU']:
    torch.cuda.manual_seed_all(params['randomSeed'])

# Setup dataloader
splits = ['train', 'val', 'test']

dataset = VisDialDataset(params, splits)

# Params to transfer from dataset
transfer = ['vocabSize', 'numOptions', 'numRounds']
for key in transfer:
    if hasattr(dataset, key):
        params[key] = getattr(dataset, key)

# Create save path and checkpoints folder
os.makedirs('checkpoints', exist_ok=True)
os.mkdir(params['savePath'])

# Loading Modules
parameters = []
aBot = None
qBot = None
aqmBot = None

# Loading A-Bot
if params['trainMode'] in ['sl-abot', 'rl-full-QAf', 'aqmbot-dep']:
    aBot, loadedParams, optim_state = utils.loadModel(params, 'abot')
    for key in loadedParams:
        params[key] = loadedParams[key]
    if params['trainMode'] not in ['aqmbot-dep']:
        parameters.extend(aBot.parameters())
    # aBot = nn.DataParallel(aBot)



# Loading Q-Bot
if params['trainMode'] in ['sl-qbot', 'rl-full-QAf']:
    qBot, loadedParams, optim_state = utils.loadModel(params, 'qbot')
    for key in loadedParams:
        params[key] = loadedParams[key]

    # Filtering parameters which require a gradient update
    parameters.extend(filter(lambda p: p.requires_grad, qBot.parameters()))
    # parameters.extend(qBot.parameters())
    # qBot = nn.DataParallel(qBot)

# Loading AQM-Bot
if params['trainMode'] in ['aqmbot-ind', 'aqmbot-dep']:
    aqmBot, loadedParams, optim_state = utils.loadModel(params, 'AQM-qbot')
    for key in loadedParams:
        params[key] = loadedParams[key]

    # Filtering parameters which require a gradient update
    parameters.extend(filter(lambda p: p.requires_grad, aqmBot.parameters()))
    # parameters.extend(qBot.parameters())
    # aqmBot = nn.DataParallel(aqmBot)

# Setup pytorch dataloader
dataset.split = 'train'
dataloader = DataLoader(
    dataset,
    batch_size=params['batchSize'],
    shuffle=False,
    num_workers=params['numWorkers'],
    drop_last=True,
    collate_fn=dataset.collate_fn,
    pin_memory=False)

# Initializing visdom environment for plotting data
viz = VisdomVisualize(
    enable=bool(params['enableVisdom']),
    env_name=params['visdomEnv'],
    server=params['visdomServer'],
    port=params['visdomServerPort'])
pprint.pprint(params)
viz.addText(pprint.pformat(params, indent=4))

# Setup optimizer
if params['continue']:
    # Continuing from a loaded checkpoint restores the following
    startIterID = params['ckpt_iterid'] + 1  # Iteration ID
    lRate = params['ckpt_lRate']  # Learning rate
    print("Continuing training from iterId[%d]" % startIterID)
else:
    # Beginning training normally, without any checkpoint
    lRate = params['learningRate']
    startIterID = 0

optimizer = optim.Adam(parameters, lr=lRate)
if params['continue']:  # Restoring optimizer state
    print("Restoring optimizer state dict from checkpoint")
    optimizer.load_state_dict(optim_state)
runningLoss = None

mse_criterion = nn.MSELoss(size_average=True, reduce=True)

numIterPerEpoch = dataset.numDataPoints['train'] // params['batchSize']
print('\n%d iter per epoch.' % numIterPerEpoch)

if params['useCurriculum']:
    if params['continue']:
        rlRound = max(0, 9 - (startIterID // numIterPerEpoch))
    else:
        rlRound = params['numRounds'] - 1
else:
    rlRound = 0

#---------------------------------------------------------------------------------------
# Training
#---------------------------------------------------------------------------------------


def batch_iter(dataloader):
    for epochId in range(params['numEpochs']):
        for idx, batch in enumerate(dataloader):
            if params['trainSplit'] is not None:
                if (params['trainSplit'] == 'last' and idx < numIterPerEpoch // 2) or \
                    (params['trainSplit'] == 'first' and idx >= numIterPerEpoch // 2):
                    continue
            yield epochId, idx, batch


start_t = timer()
for epochId, idx, batch in batch_iter(dataloader):
    # Keeping track of iterId and epoch
    iterId = startIterID + idx + (epochId * numIterPerEpoch)
    epoch = iterId // numIterPerEpoch
    gc.collect()

    # Moving current batch to GPU, if available
    if dataset.useGPU:
        batch = {key: v.cuda() if hasattr(v, 'cuda') \
                                    else v for key, v in batch.items()}

    image = Variable(batch['img_feat'], requires_grad=False)
    caption = Variable(batch['cap'], requires_grad=False)
    captionLens = Variable(batch['cap_len'], requires_grad=False)
    gtQuestions = Variable(batch['ques'], requires_grad=False)
    gtQuesLens = Variable(batch['ques_len'], requires_grad=False)
    gtAnswers = Variable(batch['ans'], requires_grad=False)
    gtAnsLens = Variable(batch['ans_len'], requires_grad=False)
    options = Variable(batch['opt'], requires_grad=False)
    optionLens = Variable(batch['opt_len'], requires_grad=False)
    gtAnsId = Variable(batch['ans_id'], requires_grad=False)

    # Initializing optimizer and losses
    optimizer.zero_grad()
    loss = 0
    qBotLoss = 0
    aBotLoss = 0
    aqmBotLoss = 0
    rlLoss = 0
    featLoss = 0
    qBotRLLoss = 0
    aBotRLLoss = 0
    predFeatures = None
    initialGuess = None
    numRounds = params['numRounds']
    # numRounds = 1 # Override for debugging lesser rounds of dialog

    # Setting training modes for both bots and observing captions, images where needed
    if aBot: 
        if params['trainMode'] in ['aqmbot-ind']:
            aBot.eval(), aBot.reset()
        else:
            aBot.train(), aBot.reset()
        aBot.observe(-1, image=image, caption=caption, captionLens=captionLens)
    if qBot:
        qBot.train(), qBot.reset()
        qBot.observe(-1, caption=caption, captionLens=captionLens)
    if aqmBot:
        aqmBot.train(), aqmBot.reset()
        aqmBot.observe(-1, caption=caption, captionLens=captionLens, image=image)

    # Q-Bot image feature regression ('guessing') only occurs if Q-Bot is present
    if params['trainMode'] in ['sl-qbot', 'rl-full-QAf']:
        initialGuess = qBot.predictImage()
        prevFeatDist = mse_criterion(initialGuess, image)
        featLoss += prevFeatDist

    if params['trainMode'] in ['aqmbot-ind']:
        initialGuess = aqmBot.predictImage()
        prevFeatDist = mse_criterion(initialGuess, image)
        featLoss += prevFeatDist

    # Iterating over dialog rounds
    for round in range(numRounds):
        '''
        Loop over rounds of dialog. Currently three modes of training are
        supported:

            sl-abot :
                Supervised pre-training of A-Bot model using cross
                entropy loss with ground truth answers

            sl-qbot :
                Supervised pre-training of Q-Bot model using cross
                entropy loss with ground truth questions for the
                dialog model and mean squared error loss for image
                feature regression (i.e. image prediction)

            rl-full-QAf :
                RL-finetuning of A-Bot and Q-Bot in a cooperative
                setting where the common reward is the difference
                in mean squared error between the current and
                previous round of Q-Bot's image prediction.

                Annealing: In order to ease in the RL objective,
                fine-tuning starts with first N-1 rounds of SL
                objective and last round of RL objective - the
                number of RL rounds are increased by 1 after
                every epoch until only RL objective is used for
                all rounds of dialog.
            
            aqmbot-ind :
                Training AQM bot with ind setting. 

        '''
        # Tracking components which require a forward pass
        # A-Bot dialog model
        forwardABot = (params['trainMode'] == 'sl-abot'
                       or (params['trainMode'] == 'rl-full-QAf'
                           and round < rlRound))
        # Q-Bot dialog model
        forwardQBot = (params['trainMode'] == 'sl-qbot'
                       or (params['trainMode'] == 'rl-full-QAf'
                           and round < rlRound))

        forwardAQMBot = params['trainMode'] in ['aqmbot-ind', 'aqmbot-dep']
        # Q-Bot feature regression network
        forwardFeatNet = (forwardQBot or params['trainMode'] == 'rl-full-QAf'
                            or forwardAQMBot)

        # Answerer Forward Pass
        if forwardABot:
            # Observe Ground Truth (GT) question
            aBot.observe(
                round,
                ques=gtQuestions[:, round],
                quesLens=gtQuesLens[:, round])
            # Observe GT answer for teacher forcing
            aBot.observe(
                round,
                ans=gtAnswers[:, round],
                ansLens=gtAnsLens[:, round])
            ansLogProbs = aBot.forward()
            # Cross Entropy (CE) Loss for Ground Truth Answers
            aBotLoss += utils.maskedNll(ansLogProbs,
                                        gtAnswers[:, round].contiguous())

        # Questioner Forward Pass (dialog model)
        if forwardQBot:
            # Observe GT question for teacher forcing
            qBot.observe(
                round,
                ques=gtQuestions[:, round],
                quesLens=gtQuesLens[:, round])
            quesLogProbs = qBot.forward()
            # Cross Entropy (CE) Loss for Ground Truth Questions
            qBotLoss += utils.maskedNll(quesLogProbs,
                                        gtQuestions[:, round].contiguous())
            # Observe GT answer for updating dialog history
            qBot.observe(
                round,
                ans=gtAnswers[:, round],
                ansLens=gtAnsLens[:, round])
        
        #### Never train qBot now
        if forwardAQMBot:
            # Observe GT question for teacher forcing
            aqmBot.observe(
                round,
                ques=gtQuestions[:, round],
                quesLens=gtQuesLens[:, round])
            if params['trainMode'] in ['aqmbot-dep']:
                aBot.observe(round, ques=gtQuestions[:, round], quesLens=gtQuesLens[:, round])
            quesLogProbs = aqmBot.forward()
            # Cross Entropy (CE) Loss for Ground Truth Questions
            '''
            aqmBotLoss += utils.maskedNll(quesLogProbs,
                                        gtQuestions[:, round].contiguous())
            '''
            # Observe GT answer for updating dialog history
            if params['trainMode'] in ['aqmbot-ind']:
                gtAns = gtAnswers[:, round]
                gtLens = gtAnsLens[:, round]
            else:
                gtAns, gtLens = aBot.forwardDecode(inference='greedy', beamSize=1)
                aBot.observe(round, ans=gtAns, ansLens=gtLens)
            aqmBot.observe(
                round,
                ans=gtAns,
                ansLens=gtLens)
            appAnsLogProbs = aqmBot.aForward() 
            aqmBotLoss  += utils.maskedNll(appAnsLogProbs,
                                        gtAns.contiguous())
            

        # In order to stay true to the original implementation, the feature
        # regression network makes predictions before dialog begins and for
        # the first 9 rounds of dialog. This can be set to 10 if needed.
        MAX_FEAT_ROUNDS = 9

        # Questioner feature regression network forward pass
        if forwardFeatNet and round < MAX_FEAT_ROUNDS and not forwardAQMBot:
            # Make an image prediction after each round
            predFeatures = qBot.predictImage()
            featDist = mse_criterion(predFeatures, image)
            featLoss += featDist
        
        #### Never train qBot now
        if forwardAQMBot and forwardFeatNet:
            '''
            predFeatures = aqmBot.predictImage()
            featDist = mse_criterion(predFeatures, image)
            featLoss += featDist
            '''

        # A-Bot and Q-Bot interacting in RL rounds
        if params['trainMode'] == 'rl-full-QAf' and round >= rlRound:
            # Run one round of conversation
            questions, quesLens = qBot.forwardDecode(inference='sample')
            qBot.observe(round, ques=questions, quesLens=quesLens)
            aBot.observe(round, ques=questions, quesLens=quesLens)
            answers, ansLens = aBot.forwardDecode(inference='sample')
            aBot.observe(round, ans=answers, ansLens=ansLens)
            qBot.observe(round, ans=answers, ansLens=ansLens)

            # Q-Bot makes a guess at the end of each round
            predFeatures = qBot.predictImage()

            # Computing reward based on Q-Bot's predicted image
            featDist = mse_criterion(predFeatures, image)

            reward = prevFeatDist.detach() - featDist
            prevFeatDist = featDist

            qBotRLLoss = qBot.reinforce(reward)
            if params['rlAbotReward']:
                aBotRLLoss = aBot.reinforce(reward)
            rlLoss += torch.mean(aBotRLLoss)
            rlLoss += torch.mean(qBotRLLoss)

    # Loss coefficients
    rlCoeff = 1
    rlLoss = rlLoss * rlCoeff
    featLoss = featLoss * params['featLossCoeff']
    # Averaging over rounds
    qBotLoss = (params['CELossCoeff'] * qBotLoss) / numRounds
    aBotLoss = (params['CELossCoeff'] * aBotLoss) / numRounds
    aqmBotLoss = (params['CELossCoeff'] * aqmBotLoss) / numRounds
    featLoss = featLoss / numRounds  #/ (numRounds+1)
    rlLoss = rlLoss / numRounds
    # Total loss
    loss = qBotLoss + aBotLoss + rlLoss + featLoss + aqmBotLoss
    loss.backward()
    optimizer.step()

    # Tracking a running average of loss
    if runningLoss is None:
        runningLoss = loss.data[0]
    else:
        runningLoss = 0.95 * runningLoss + 0.05 * loss.data[0]

    # Decay learning rate
    if lRate > params['minLRate']:
        for gId, group in enumerate(optimizer.param_groups):
            optimizer.param_groups[gId]['lr'] *= params['lrDecayRate']
        lRate *= params['lrDecayRate']
        if iterId % 10 == 0:  # Plot learning rate till saturation
            viz.linePlot(iterId, lRate, 'learning rate', 'learning rate')

    # RL Annealing: Every epoch after the first, decrease rlRound
    if iterId % numIterPerEpoch == 0 and iterId > 0:
        if params['trainMode'] == 'rl-full-QAf':
            rlRound = max(0, rlRound - 1)
            print('Using rl starting at round {}'.format(rlRound))

    # Print every now and then
    if iterId % 10 == 0:
        end_t = timer()  # Keeping track of iteration(s) time
        curEpoch = float(iterId) / numIterPerEpoch
        timeStamp = strftime('%a %d %b %y %X', gmtime())
        printFormat = '[%s][Ep: %.2f][Iter: %d][Time: %5.2fs][Loss: %.3g]'
        printFormat += '[lr: %.3g]'
        printInfo = [
            timeStamp, curEpoch, iterId, end_t - start_t, loss.data[0], lRate
        ]
        start_t = end_t
        print(printFormat % tuple(printInfo))

        # Update line plots
        if isinstance(aBotLoss, Variable):
            viz.linePlot(iterId, aBotLoss.data[0], 'aBotLoss', 'train CE')
        if isinstance(qBotLoss, Variable):
            viz.linePlot(iterId, qBotLoss.data[0], 'qBotLoss', 'train CE')
        if isinstance(rlLoss, Variable):
            viz.linePlot(iterId, rlLoss.data[0], 'rlLoss', 'train')
        if isinstance(featLoss, Variable):
            viz.linePlot(iterId, featLoss.data[0], 'featLoss',
                         'train FeatureRegressionLoss')
        viz.linePlot(iterId, loss.data[0], 'loss', 'train loss')
        viz.linePlot(iterId, runningLoss, 'loss', 'running train loss')

    # Evaluate every epoch
    if iterId % (numIterPerEpoch // 1) == 0 or (params['trainSplit'] == 'last' and \
        (iterId - numIterPerEpoch // 2) % (numIterPerEpoch // 1) == 0):
        # Keeping track of epochID
        curEpoch = float(iterId) / numIterPerEpoch
        epochId = (1.0 * iterId / numIterPerEpoch) + 1

        # Set eval mode
        if aBot and aBot.training:
            aBot.eval()
        if qBot:
            qBot.eval()

        if params['enableVisdom']:
            # Printing visdom environment name in terminal
            print("Currently on visdom env [%s]" % (params['visdomEnv']))

        # Mapping iteration count to epoch count
        viz.linePlot(iterId, epochId, 'iter x epoch', 'epochs')

        print('Performing validation...')
        if aBot and params['trainMode'] in ['sl-abot', 'rl-full-QAf'] and 'ques' in batch:
            print("aBot Validation:")

            # NOTE: A-Bot validation is slow, so adjust exampleLimit as needed
            rankMetrics = rankABot(
                aBot,
                dataset,
                'val',
                scoringFunction=utils.maskedNll,
                exampleLimit=25 * params['batchSize'])

            for metric, value in rankMetrics.items():
                viz.linePlot(
                    epochId, value, 'val - aBot', metric, xlabel='Epochs')

            if 'logProbsMean' in rankMetrics:
                logProbsMean = params['CELossCoeff'] * rankMetrics[
                    'logProbsMean']
                viz.linePlot(iterId, logProbsMean, 'aBotLoss', 'val CE')

                if params['trainMode'] == 'sl-abot':
                    valLoss = logProbsMean
                    viz.linePlot(iterId, valLoss, 'loss', 'val loss')

        if qBot:
            print("qBot Validation:")
            rankMetrics, roundMetrics = rankQBot(qBot, dataset, 'val')

            for metric, value in rankMetrics.items():
                viz.linePlot(
                    epochId, value, 'val - qBot', metric, xlabel='Epochs')

            viz.linePlot(iterId, epochId, 'iter x epoch', 'epochs')

            if 'logProbsMean' in rankMetrics:
                logProbsMean = params['CELossCoeff'] * rankMetrics[
                    'logProbsMean']
                viz.linePlot(iterId, logProbsMean, 'qBotLoss', 'val CE')

            if 'featLossMean' in rankMetrics:
                featLossMean = params['featLossCoeff'] * (
                    rankMetrics['featLossMean'])
                viz.linePlot(iterId, featLossMean, 'featLoss',
                             'val FeatureRegressionLoss')

            if 'logProbsMean' in rankMetrics and 'featLossMean' in rankMetrics:
                if params['trainMode'] == 'sl-qbot':
                    valLoss = logProbsMean + featLossMean
                    viz.linePlot(iterId, valLoss, 'loss', 'val loss')

    # Save the model after every epoch
    if iterId % numIterPerEpoch == 0 or (params['trainSplit'] == 'last' and idx == numIterPerEpoch // 2):
        params['ckpt_iterid'] = iterId
        params['ckpt_lRate'] = lRate

        if aBot and params['trainMode'] in ['sl-abot', 'rl-full-QAf']:
            saveFile = os.path.join(params['savePath'],
                                    'abot_ep_%d.vd' % curEpoch)
            print('Saving model: ' + saveFile)
            utils.saveModel(aBot, optimizer, saveFile, params)
        if qBot:
            saveFile = os.path.join(params['savePath'],
                                    'qbot_ep_%d_q.vd' % curEpoch)
            utils.saveModel(qBot, optimizer, saveFile, params)
        if aqmBot:
            saveFile = os.path.join(params['savePath'],
                                    'aqmbot_ep_%d_q.vd' % curEpoch)
            utils.saveModel(aqmBot, optimizer, saveFile, params)


