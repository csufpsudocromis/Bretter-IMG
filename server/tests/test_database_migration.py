import unittest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import machine_group, machine, image, job, user  # noqa: F401
from app.models.job import Job, JobType, JobStatus
from app.models.machine import Machine


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_jobs_table_has_command_interactive_column(self):
        inspector = inspect(self.engine)
        columns = {col["name"] for col in inspector.get_columns("jobs")}
        self.assertIn("command_interactive", columns)

    def test_create_and_query_job_with_command_interactive(self):
        machine_record = Machine(id="test-machine-id", hostname="test-host")
        self.db.add(machine_record)
        self.db.commit()

        new_job = Job(
            id="test-job-id",
            type=JobType.remote_command,
            status=JobStatus.pending,
            machine_id="test-machine-id",
            command_shell="powershell",
            command_text="Get-Process",
            command_timeout_seconds=60,
            command_interactive="false",
        )
        self.db.add(new_job)
        self.db.commit()

        queried = self.db.query(Job).filter(Job.id == "test-job-id").one()
        self.assertEqual(queried.command_interactive, "false")
        self.assertEqual(queried.command_shell, "powershell")

    def test_job_create_and_agent_out_schemas(self):
        from app.schemas.job import JobCreate, AgentJobOut
        from datetime import datetime

        create_data = JobCreate(
            type=JobType.remote_command,
            machine_ids=["test-machine-id"],
            command_shell="cmd",
            command_text="start notepad.exe",
            command_timeout_seconds=30,
            command_interactive=True,
        )
        self.assertTrue(create_data.command_interactive)

        job_row = Job(
            id="test-job-interactive",
            type=JobType.remote_command,
            status=JobStatus.pending,
            machine_id="test-machine-id",
            command_shell="cmd",
            command_text="start notepad.exe",
            command_timeout_seconds=30,
            command_interactive="true",
            created_at=datetime.utcnow(),
            remove_winpe_after_deploy="false",
            restore_hostname_after_deploy="false",
            sysprep_before_capture="false",
        )
        self.db.add(job_row)
        self.db.commit()

        agent_out = AgentJobOut.model_validate(job_row)
        self.assertTrue(agent_out.command_interactive)
        self.assertEqual(agent_out.command_shell, "cmd")


if __name__ == "__main__":
    unittest.main()
