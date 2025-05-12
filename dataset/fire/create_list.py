import random
import os

random.seed(100)
data_root_dir = "fireOrigin"
annotate_root_dir = "annotations"
image_root_dir = "images"

itemList = []
for ip in os.listdir(os.path.join(data_root_dir, image_root_dir)):
    ap = ip.replace("jpg", "xml")
    print(ip + " " + ap)
    itemList.append(
        (os.path.join(data_root_dir, image_root_dir, ip), os.path.join(data_root_dir, annotate_root_dir, ap)))

ratio = 0.9
random.shuffle(itemList)
trainf = open(os.path.join("trainval.txt"), "w")
testf = open(os.path.join("test.txt"), "w")

for i, item in enumerate(itemList):
    img, annot = item[0], item[1]
    writeText = img + ' ' + annot + '\n'

    if i < ratio * len(itemList):
        trainf.write(writeText)
    else:
        testf.write(writeText)

trainf.close()
testf.close()
