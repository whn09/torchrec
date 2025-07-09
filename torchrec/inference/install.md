```
export MY_INSTALL_DIR=$HOME/.local
mkdir -p $MY_INSTALL_DIR
export PATH="$MY_INSTALL_DIR/bin:$PATH"

sudo apt install -y cmake

sudo apt install -y build-essential autoconf libtool pkg-config

git clone --recurse-submodules -b v1.73.0 --depth 1 --shallow-submodules https://github.com/grpc/grpc

cd grpc
mkdir -p cmake/build
pushd cmake/build
cmake -DgRPC_INSTALL=ON \
      -DgRPC_BUILD_TESTS=OFF \
      -DCMAKE_CXX_STANDARD=17 \
      -DCMAKE_INSTALL_PREFIX=$MY_INSTALL_DIR \
      ../..
make -j 4
make install
popd

which protoc  # Should output $HOME/.local/bin

pip install torch --index-url https://download.pytorch.org/whl/cu126
pip install fbgemm-gpu --index-url https://download.pytorch.org/whl/cu126
pip install torchmetrics==1.0.3
pip install torchrec --index-url https://download.pytorch.org/whl/cu126

find /opt/pytorch/lib/python3.12/site-packages/ -name fbgemm_gpu_py.so
export FBGEMM_LIB=/opt/pytorch/lib/python3.12/site-packages/fbgemm_gpu/fbgemm_gpu_py.so

export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/pytorch/lib/python3.12/site-packages/torch/lib/
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/pytorch/lib/python3.12/site-packages/fbgemm_gpu/

git clone https://github.com/pytorch/torchrec.git

cd ~/torchrec/torchrec/inference/
python dlrm_packager.py --output_path /tmp/model.pt

pip install grpcio-tools
python -m grpc_tools.protoc -I protos --python_out=. --grpc_python_out=. protos/predictor.proto

# Change some fixed paths in CMakeLists.txt
cmake -S . -B build/ -DCMAKE_PREFIX_PATH="$(python -c 'import torch.utils; print(torch.utils.cmake_prefix_path)');" -DFBGEMM_LIB="$FBGEMM_LIB"

cd build
make -j

./server /tmp/model.pt

python client.py
```