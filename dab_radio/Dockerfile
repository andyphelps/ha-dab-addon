ARG BUILD_FROM
FROM ${BUILD_FROM}

# welle-cli build deps. Deliberately no librtlsdr-dev/libusb (rtl_tcp.cpp is
# an unconditional input backend, not gated behind the RTLSDR cmake option --
# this container never touches USB, only TCP to a remote rtl_tcp) and no
# libasound2-dev (ALSA is a soft find_package, not required).
RUN apt-get update && apt-get install -y --no-install-recommends \
      cmake make g++ git pkg-config ca-certificates xxd \
      libmp3lame-dev libfftw3-dev libfaad-dev libmpg123-dev \
      python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone --depth 1 --branch v2.7 https://github.com/AlbrechtL/welle.io.git . \
    && cmake -S . -B build \
        -DBUILD_WELLE_IO=OFF \
        -DBUILD_WELLE_CLI=ON \
        -DRTLSDR=OFF \
        -DFLAC=OFF \
        -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build -j"$(nproc)" --target welle-cli \
    && cp build/welle-cli /usr/local/bin/welle-cli \
    && cd / && rm -rf /build

RUN pip3 install --break-system-packages --no-cache-dir flask requests

COPY controller.py /usr/src/controller.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

EXPOSE 8080 9000
CMD ["/run.sh"]
