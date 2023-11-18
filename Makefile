#!/usr/bin/env make

build:
	docker build -t payments-service:latest .

run:
	docker run --network=host --env-file=.env -it payments-service:latest

run-bg:
	docker run --network=host --env-file=.env -d payments-service:latest
