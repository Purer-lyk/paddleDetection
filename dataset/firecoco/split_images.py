import os
import shutil

vocFolder = "../fire"
cocoTrainFolder = "train_fire"
cocoValFolder = "val_fire"

trainTxt = open(os.path.join(vocFolder, "trainval.txt"), 'r')
testTxt = open(os.path.join(vocFolder, "test.txt"), 'r')

for trainf in os.listdir(cocoTrainFolder):
    os.remove(os.path.join(cocoTrainFolder, trainf))

for valf in os.listdir(cocoValFolder):
    os.remove(os.path.join(cocoValFolder, valf))

for line in trainTxt.readlines():
    context = line.split(' ')
    fileName = context[0].split('\\')[-1]
    vocPath = os.path.join(vocFolder, context[0])
    cocoPath = os.path.join(cocoTrainFolder, fileName)
    shutil.copy(vocPath, cocoPath)

for line in testTxt.readlines():
    context = line.split(' ')
    fileName = context[0].split('\\')[-1]
    vocPath = os.path.join(vocFolder, context[0])
    cocoPath = os.path.join(cocoValFolder, fileName)
    shutil.copy(vocPath, cocoPath)