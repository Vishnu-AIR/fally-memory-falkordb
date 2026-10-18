COMPOSE = docker compose

up:            ## start FalkorDB + API on http://localhost:8000
	$(COMPOSE) up -d --build

seed:          ## load the synthetic users (one FalkorDB graph each)
	$(COMPOSE) exec api python -m scripts.seed

test:          ## run the tests against the FalkorDB container
	$(COMPOSE) exec api pytest -q

down:
	$(COMPOSE) down

demo-aws:      ## the live demo (Caddy + HTTPS); needs DOMAIN
	DOMAIN=$(DOMAIN) $(COMPOSE) -f docker-compose.yml -f docker-compose.aws.yml up -d --build
