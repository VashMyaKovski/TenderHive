#!/bin/sh
set -eu

for topic in \
    tender.job.created \
    tender.stage.done \
    tender.agent.completed \
    tender.human.required \
    tender.human.decided \
    tender.artifacts.ready
do
    /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server "$KAFKA_BOOTSTRAP_SERVERS" \
        --create --if-not-exists --topic "$topic" --partitions 1 --replication-factor 1
done
