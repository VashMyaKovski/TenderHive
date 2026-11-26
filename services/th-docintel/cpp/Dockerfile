FROM debian:bookworm-slim AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        cmake \
        libgrpc++-dev \
        libprotobuf-dev \
        nlohmann-json3-dev \
        protobuf-compiler \
        protobuf-compiler-grpc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY cpp ./cpp
COPY proto ./proto
COPY templates ./templates
RUN cmake -S cpp -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DTH_DOCINTEL_BUILD_BENCHMARKS=OFF \
    && cmake --build build --parallel

FROM build AS test
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgtest-dev \
    && rm -rf /var/lib/apt/lists/* \
    && cmake -S cpp -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DTH_DOCINTEL_BUILD_BENCHMARKS=ON \
    && cmake --build build --parallel \
    && ctest --test-dir build --output-on-failure
CMD ["ctest", "--test-dir", "/src/build", "--output-on-failure"]

FROM debian:bookworm-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libgrpc++1.51 \
        libprotobuf32 \
        passwd \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home docintel

WORKDIR /app
COPY --from=build /src/build/docintel-core /app/docintel-core
COPY --from=build /src/templates /app/templates
USER 10001:10001
EXPOSE 50051
ENTRYPOINT ["/app/docintel-core"]
