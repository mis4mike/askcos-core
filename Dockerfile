ARG BASE_VERSION=old
ARG DATA_VERSION=old

FROM fourthievesvinegar/askcos-data:$DATA_VERSION as data

FROM fourthievesvinegar/askcos-base:$BASE_VERSION

RUN apt-get update && \
    apt-get install -y libboost-thread-dev libboost-python-dev libboost-iostreams-dev python-tk libopenblas-dev libeigen3-dev libcairo2-dev pkg-config python-dev python-mysqldb && \
    useradd -ms /bin/bash askcos

RUN python -m pip install --upgrade pip

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && rm requirements.txt

# Use non-AVX version of tensorflow
COPY tensorflow-2.0.0a0-cp37-cp37m-linux_x86_64.whl tensorflow-2.0.0a0-cp37-cp37m-linux_x86_64.whl
RUN pip install tensorflow-2.0.0a0-cp37-cp37m-linux_x86_64.whl

COPY --from=data /data /usr/local/askcos-core/askcos/data

COPY --chown=askcos:askcos . /usr/local/askcos-core

WORKDIR /home/askcos
USER askcos

ENV PYTHONPATH=/usr/local/askcos-core:${PYTHONPATH}

LABEL core.version={VERSION} \
      core.git.hash={GIT_HASH} \
      core.git.date={GIT_DATE} \
      core.git.describe={GIT_DESCRIBE}
