#!/bin/bash

set -e
TMPDIR=/tmp
sudo apt-get update && sudo apt-get install -y jq wget
sudo pip3 install torch-model-archiver
cp torchserve/custom_handler.py $TMPDIR/
cd $TMPDIR
# TODO: use gamutRF weights here.
wget https://github.com/ultralytics/assets/releases/download/v0.0.0/yolov8n.pt
wget https://raw.githubusercontent.com/pytorch/serve/master/examples/object_detector/yolo/yolov8/requirements.txt
torch-model-archiver --force --model-name yolov8n --version 1.0 --serialized-file yolov8n.pt --handler custom_handler.py -r requirements.txt
rm -rf model_store && mkdir model_store
mv yolov8n.mar model_store/
# TODO: --runtime nvidia is required for Orin
docker run -v $(pwd)/model_store:/model_store -p 8080:8080 --rm --name testts --entrypoint timeout -d iqtlabs/torchserve 180s /torchserve/torchserve-entrypoint.sh --models yolov8n=yolov8n.mar
# TODO: use gamutRF test spectogram image
wget https://github.com/pytorch/serve/raw/master/examples/object_detector/yolo/yolov8/persons.jpg
wget -q --retry-connrefused --retry-on-host-error --body-file=persons.jpg --method=PUT -O- --header='Content-Type: image/jpg' http://127.0.0.1:8080/predictions/yolov8n | jq
