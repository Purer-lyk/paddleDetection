import os

raw_images_dir = "rawImg"
fire_origin_dir = "fireOrigin/images"
index = 600

for img in os.listdir(raw_images_dir):
    print(img)
    os.rename(os.path.join(raw_images_dir, img), os.path.join(raw_images_dir, str(index) + '.jpg'))
    index += 1

for img in os.listdir(raw_images_dir):
    print(img)
