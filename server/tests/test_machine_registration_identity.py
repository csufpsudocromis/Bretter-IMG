import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.machines import register_machine
from app.core.database import Base
from app.models import image, job, machine, machine_group, user  # noqa: F401
from app.models.job import Job, JobType
from app.models.machine import Machine, MachineStatus
from app.schemas.machine import MachineRegister


class MachineRegistrationIdentityTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()

    def test_reused_machine_id_with_different_mac_gets_reassigned(self):
        shared_id = "shared-installer-machine-id"

        first = register_machine(
            MachineRegister(
                id=shared_id,
                hostname="MCBE-Test1",
                mac_address="00:11:22:33:44:55",
                agent_version="1.0.10",
            ),
            db=self.db,
        )
        self.assertEqual(first.id, shared_id)
        self.assertIsNone(first.agent_token)

        second = register_machine(
            MachineRegister(
                id=shared_id,
                hostname="MCBE-Test2",
                mac_address="66:77:88:99:aa:bb",
                agent_version="1.0.9",
            ),
            db=self.db,
        )
        self.assertNotEqual(second.id, shared_id)
        self.assertTrue(second.agent_token)

        rows = self.db.query(Machine).order_by(Machine.hostname).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.hostname for row in rows}, {"MCBE-Test1", "MCBE-Test2"})
        self.assertEqual(
            self.db.query(Machine).filter(Machine.id == shared_id).one().status,
            MachineStatus.offline,
        )
        compatibility_updates = (
            self.db.query(Job)
            .filter(Job.machine_id == shared_id, Job.type == JobType.update_agent)
            .count()
        )
        self.assertEqual(compatibility_updates, 1)

        repeat = register_machine(
            MachineRegister(
                id=shared_id,
                hostname="MCBE-Test2",
                mac_address="66:77:88:99:aa:bb",
                agent_version="1.0.9",
            ),
            db=self.db,
        )
        self.assertEqual(repeat.id, second.id)
        self.assertEqual(self.db.query(Machine).count(), 2)


if __name__ == "__main__":
    unittest.main()
