#!/bin/bash
cd ..
rsync --progress --delete --exclude bbs2/vue-app/node_modules --exclude bbs2/.v --exclude bbs2/config --exclude bbs2/data -a bbs2 radiostation2:
