-- Runs once, on first start of the docker-compose Postgres volume. The eval harness also
-- creates booking_agent_eval itself if it's missing, so older volumes keep working.
CREATE DATABASE booking_agent_test OWNER booking;
CREATE DATABASE booking_agent_eval OWNER booking;
