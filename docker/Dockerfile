FROM python:3.11.11-slim-bullseye AS builder

WORKDIR /app

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
  && apt-get -qqq install --no-install-recommends -y pkg-config gcc g++ \
  && apt-get upgrade --assume-yes \
  && apt-get clean \
  && rm -rf /var/lib/apt

RUN python -mvenv venv && ./venv/bin/pip install --no-cache-dir --upgrade pip

COPY . .

# Install package from source code, compile translations
RUN ./venv/bin/pip cache purge
RUN ./venv/bin/pip install Babel==2.12.1
RUN ./venv/bin/pip install torch==2.2.0 --extra-index-url https://download.pytorch.org/whl/cpu
RUN ./venv/bin/pip install "numpy<2"
RUN ./venv/bin/pip install .
RUN ./venv/bin/pip cache purge

FROM python:3.11.11-slim-bullseye

ARG with_models=false
ARG models=""

RUN addgroup --system --gid 1032 libretranslate && adduser --system --uid 1032 libretranslate && mkdir -p /home/libretranslate/.local && chown -R libretranslate:libretranslate /home/libretranslate/.local
USER libretranslate

COPY --from=builder --chown=1032:1032 /app /app
WORKDIR /app

COPY --from=builder --chown=1032:1032 /app/venv/bin/ltmanage /usr/bin/

RUN ./venv/bin/python scripts/install_models.py --load_only_lang_codes "en,es,fr,de,ar,pt,bn,hi,ur,ru,zh-Hans,zh-Hant";

EXPOSE 5000
ENTRYPOINT [ "./venv/bin/libretranslate", "--host", "*" ]
