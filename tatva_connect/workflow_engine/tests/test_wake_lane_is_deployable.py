# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE WAKE LANE MUST BE A REPO ARTIFACT, OR THE ALARM SILENTLY NEVER FIRES.

W4.1 put the timer accelerator on a queue Frappe's own scheduler does not manage. That lane only exists
if the bench DECLARES it in `common_site_config.workers`. It was registered by hand on the dev bench and
nowhere else, which is the exact booby trap W4 §5.1 documents: `enqueue_at` succeeds, returns a job, and
nothing ever executes it. No exception, no log line, no failed job — the run just waits for the sweep.

`partner_bulk` had the same gap and is fixed by the same line.

This walks the compose file the bench is really built from rather than asserting against live config: a
value present only on this machine is precisely what the chunk got wrong.
"""
import pathlib
import re
import unittest

from tatva_connect.workflow_engine import wakeups

_COMPOSE = pathlib.Path(__file__).parents[3] / ".localdev" / "compose.yml"
_DEVOPS = pathlib.Path(__file__).parents[3] / "docs" / "prod-deploy" / "DEVOPS.md"


class TestTheLaneIsDeclaredWhereTheBenchIsBuilt(unittest.TestCase):
	def setUp(self):
		if not _COMPOSE.exists():
			self.skipTest("compose file not present in this checkout")
		self.compose = _COMPOSE.read_text()

	def test_the_configurator_registers_the_wake_lane(self):
		"""THE red: the lane lived only in this bench's common_site_config."""
		registration = re.search(r"set-config\s+-gp\s+workers\s+\"([^\"]+)\"", self.compose)
		self.assertIsNotNone(registration, "no worker-lane registration in the configurator")
		self.assertIn(wakeups.WAKE_QUEUE, registration.group(1))

	def test_it_registers_every_lane_a_worker_service_consumes(self):
		"""A worker on an unregistered lane consumes nothing and says nothing. Derived from the compose
		file's own worker commands, so a lane added later is covered without editing this test."""
		registration = re.search(r"set-config\s+-gp\s+workers\s+\"([^\"]+)\"", self.compose).group(1)
		builtin = {"short", "default", "long"}
		consumed = set()
		for queues in re.findall(r"bench worker --queue ([\w,]+)", self.compose):
			consumed |= set(queues.split(","))
		missing = sorted((consumed - builtin) - set(re.findall(r"'([\w_]+)':", registration)))
		self.assertEqual(missing, [], f"a worker consumes these lanes and nothing declares them: {missing}")

	def test_a_worker_and_a_scheduler_exist_for_the_lane(self):
		"""The alarm needs both: something to move due jobs onto the queue, and something to run them."""
		self.assertIn(f"bench worker --queue {wakeups.WAKE_QUEUE}", self.compose)
		self.assertIn("wakeups.run_wake_scheduler", self.compose)

	def test_the_deploy_step_is_written_down_for_benches_compose_does_not_build(self):
		"""UAT and prod are DevOps-owned and never run this compose file, so the step has to be a checklist
		item rather than folklore."""
		if not _DEVOPS.exists():
			self.skipTest("prod-deploy docs not present in this checkout")
		devops = _DEVOPS.read_text()
		self.assertIn("set-config -gp workers", devops, "the deploy step is not documented")
		self.assertIn(wakeups.WAKE_QUEUE, devops)
