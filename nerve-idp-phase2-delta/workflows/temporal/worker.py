"""
Temporal Worker — entrypoint

Registers all workflow and activity implementations
and starts a worker on the specified task queue.

Usage:
  python worker.py --queue nerve-scaffold
  python worker.py --queue nerve-iac
  python worker.py --queue nerve-runbooks
  python worker.py --queue nerve-chaos
"""

import argparse
import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def run_worker(task_queue: str) -> None:
    import os

    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost")
    temporal_port = os.environ.get("TEMPORAL_PORT", "7233")

    client = await Client.connect(f"{temporal_host}:{temporal_port}")

    # Register workflows and activities by queue
    workflows = []
    activities = []

    if task_queue == "nerve-scaffold":
        from scaffold_workflow import (
            ScaffoldWorkflow,
            validate_scaffold_request,
            render_cookiecutter_template,
            create_github_repo,
            push_initial_commit,
            configure_branch_protection,
            create_k8s_namespace_resources,
            provision_vault_secrets,
            register_service_in_catalog,
        )
        workflows = [ScaffoldWorkflow]
        activities = [
            validate_scaffold_request,
            render_cookiecutter_template,
            create_github_repo,
            push_initial_commit,
            configure_branch_protection,
            create_k8s_namespace_resources,
            provision_vault_secrets,
            register_service_in_catalog,
        ]

    elif task_queue == "nerve-iac":
        from iac_workflow import (
            IaCApplyWorkflow,
            generate_iac_plan,
            store_iac_plan_output,
            validate_iac_approver,
            apply_iac_plan,
        )
        workflows = [IaCApplyWorkflow]
        activities = [
            generate_iac_plan,
            store_iac_plan_output,
            validate_iac_approver,
            apply_iac_plan,
        ]

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
    )

    logger.info("Starting Temporal worker on queue: %s", task_queue)
    await worker.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True, help="Temporal task queue name")
    args = parser.parse_args()
    asyncio.run(run_worker(args.queue))
