# Local Docker setup for tap_lms

This guide creates a fresh local Frappe site named `tap_lms.localhost` using Docker, Postgres, Redis, and the local `tap_lms` app source from this repository.

The setup script assumes Docker is already installed. It creates the bench and site inside Docker volumes, mounts this repository into the dev container, installs `tap_lms`, and seeds the local integration settings that the app code expects.

## 1. Install Docker

Install Docker Desktop for your operating system:

- macOS: download Docker Desktop from `https://www.docker.com/products/docker-desktop/`, install it, then start Docker Desktop.
- Windows: install Docker Desktop with WSL 2 enabled, then start Docker Desktop.
- Linux: install Docker Engine and the Docker Compose plugin from your distribution package manager or Docker's official instructions.

Verify Docker is available:

```sh
docker --version
docker compose version
```

## 2. Add the local hostname

Add this line to your hosts file so the browser can resolve the local Frappe site:

```text
127.0.0.1 tap_lms.localhost
```

On macOS or Linux, edit `/etc/hosts`. On Windows, edit `C:\Windows\System32\drivers\etc\hosts` as Administrator.

## 3. Create `env.local`

Copy the example file:

```sh
cp .env.example env.local
```

Fill in at least these values:

```dotenv
SITE_NAME=tap_lms.localhost
FRAPPE_BRANCH=v14.29.0
FRAPPE_PYTHON_VERSION=3.10.20
ADMIN_PASSWORD=admin
POSTGRES_PASSWORD=postgres
BUSINESS_THEME_REPO=https://github.com/Midocean-Technologies/business_theme_v14.git
```

The RabbitMQ queue for this app is external. Log in to CloudAMQP at `https://customer.cloudamqp.com/login`, open the instance used for local testing, and copy the AMQP connection details into `env.local`.

CloudAMQP usually shows a URL like:

```text
amqps://USERNAME:PASSWORD@HOST/VIRTUAL_HOST
```

Split it into:

```dotenv
RABBITMQ_HOST=HOST
RABBITMQ_PORT=5671
RABBITMQ_VIRTUAL_HOST=VIRTUAL_HOST
RABBITMQ_USERNAME=USERNAME
RABBITMQ_PASSWORD=PASSWORD
RABBITMQ_SUBMISSION_QUEUE=your-submission-queue
RABBITMQ_PLAGIARISM_RESULTS_QUEUE=your-plagiarism-results-queue
RABBITMQ_FEEDBACK_RESULTS_QUEUE=your-feedback-results-queue
```

Keep `env.local` private. It contains credentials and should not be committed.

## 4. Start the fresh local setup

Run:

```sh
chmod +x scripts/start_local_docker.sh
./scripts/start_local_docker.sh
```

The script will:

- build a local dev image from `docker/local/Dockerfile`
- start Postgres and two Redis containers under the `tap_lms_local` Compose project
- create `/home/frappe/frappe-bench` in a Docker volume if it does not exist
- create the `tap_lms.localhost` site with Postgres
- symlink `/home/frappe/frappe-bench/apps/tap_lms` to the mounted local repository at `/workspace/tap_lms`
- install the local `tap_lms` app from that mounted path
- install `business_theme_v14` from the configured theme repository
- run migrations
- seed `RabbitMQ Settings`, `GCS Settings`, `ElevenLabs Settings`, and `VoiceAgentSettings`

If you change `FRAPPE_BRANCH`, reset the local Docker volumes before re-running
the setup so bench and site are recreated on that branch:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml down -v
```

After setup, the script also starts Frappe in the background inside the `dev`
container. If you need to start it again manually:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec -d dev bash -lc "cd /home/frappe/frappe-bench && nohup bench start > logs/local-bench-start.log 2>&1 </dev/null &"
```

Open:

```text
http://tap_lms.localhost:8000
```

Login with:

```text
User: Administrator
Password: the ADMIN_PASSWORD value from `env.local`
```

## 5. Required local DocTypes discovered in code

The active folders reviewed were:

- `tap_lms/config`
- `tap_lms/feedback_handler`
- `tap_lms/imgana`
- `tap_lms/summer_program`
- `tap_lms/tap_lms`

The local setup needs these integration settings available:

- `RabbitMQ Settings`: used by `tap_lms/imgana/submission.py` and `tap_lms/feedback_handler/feedback_consumer.py`.
- `GCS Settings`: used by image submission upload flows in `tap_lms/imgana/submission.py`. It can stay disabled for a basic local site.
- `ElevenLabs Settings`: used by `tap_lms/feedback_handler/audio/audio_helpers.py`. It can stay disabled for a basic local site.
- `VoiceAgentSettings`: used by `tap_lms/summer_program/vocallabs.py`. It can stay disabled for a basic local site.

The script seeds those single DocTypes from `.env`. RabbitMQ should be filled when you want image submission publishing or feedback consumption to work. The other integrations can remain disabled unless you are testing those flows.

## 6. Create the API user after install

Some image submission endpoints authenticate through the custom `API Key` DocType, not Frappe's built-in API key fields. The code checks for an enabled `API Key` row where `key` matches the submitted `api_key`.

In the Frappe UI:

1. Log in as `Administrator`.
2. Open **Users** and create a user such as `local.api@tap-lms.local`.
3. Assign the roles needed for the flow you are testing. For broad local development, use `System Manager`.
4. Open **API Key**.
5. Create a new row:
   - `user`: the user you created
   - `key`: a local-only secret, for example `local-dev-api-key`
   - `enabled`: checked
6. Save the document.

Use that `key` value as the `api_key` parameter when calling endpoints such as `tap_lms.imgana.submission.submit_artwork`.

## 7. Useful commands

Start containers:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml up -d
```

Restart after backend code changes:

```sh
./scripts/restart_local_docker.sh
```

Rebuild assets first when JS/CSS changes:

```sh
./scripts/restart_local_docker.sh --build
```

Tail runtime logs:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash -lc "tail -f /home/frappe/frappe-bench/logs/local-bench-start.log"
```

Run migrations:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash -lc "cd /home/frappe/frappe-bench && bench --site tap_lms.localhost migrate"
```

Open a bench shell:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash
```

Stop containers:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml down
```

Reset the local bench and database volumes:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml down -v
```

Only use the reset command when you are comfortable deleting the local Docker database and bench volumes.
