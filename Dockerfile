# Development build stages for the frozen kernel and SQL implementation.
FROM ubuntu:24.04@sha256:78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d AS build

RUN printf '%s\n' \
      'Types: deb' \
      'URIs: http://archive.ubuntu.com/ubuntu/' \
      'Suites: noble' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/ubuntu.sources \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update --error-on=any \
    && apt-get install --yes --no-install-recommends ca-certificates=20240203 \
    && printf '%s\n' \
      'Types: deb' \
      'URIs: https://snapshot.ubuntu.com/ubuntu/20261109T000000Z/' \
      'Suites: noble noble-updates noble-security' \
      'Components: main universe' \
      'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/ubuntu.sources \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update --error-on=any \
    && apt-get install --yes --no-install-recommends \
       g++ cmake make python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /source
COPY CMakeLists.txt ./
COPY include/ include/
COPY src/ src/
COPY tests/ tests/
COPY mysqltest/ mysqltest/
COPY protocol_check/ protocol_check/

RUN cmake -S /source -B /build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /build --parallel 2 \
    && dpkg-query -W > /build/installed-packages.tsv

FROM build AS development-checks
LABEL org.opencontainers.image.title="MiniVectorDB development checks" \
      minivectordb.phase="judge-protocol-v1" \
      minivectordb.formal-ready="true"

RUN ctest --test-dir /build --output-on-failure \
    && python3 /source/tests/public_samples.py /build/sql_public_case_driver \
       --report /build/public-samples.json \
    && python3 /source/tests/legacy_schema.py /build/sql_public_case_driver \
       --report /build/legacy-schema.json
RUN python3 /source/tests/legacy_schema.py /build/sql_public_case_driver \
       --vector-index --report /build/legacy-index-schema.json
RUN python3 /source/protocol_check/run.py \
       --command '["/build/minivectordb_development_server","--development-protocol","mvdb-judge-json-v1","--data","{data_dir}"]' \
       --output /build/protocol-results
RUN python3 /source/tests/public_protocol_check.py \
       --driver /build/minivectordb_development_server \
       --report /build/public-protocol-report.json

ENTRYPOINT ["/build/minivectordb_development_server"]
CMD ["--development-protocol", "mvdb-judge-json-v1", "--data", "/data"]
