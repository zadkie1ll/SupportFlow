from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class EmployeeRole:
    key: str
    title: str
    objective: str
    responsibilities: tuple[str, ...]


@dataclass(frozen=True)
class RoleRegistry:
    roles: dict[str, EmployeeRole]
    assignments: dict[str, str]

    def for_employee(self, employee_id: str) -> EmployeeRole:
        role_key = self.assignments.get(str(employee_id))
        if not role_key or role_key not in self.roles:
            raise KeyError(f"Role is not configured for employee {employee_id}")
        return self.roles[role_key]


@lru_cache(maxsize=1)
def load_roles() -> RoleRegistry:
    path = Path(__file__).with_name("roles.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_roles = payload.get("roles")
    raw_assignments = payload.get("assignments")
    if not isinstance(raw_roles, dict) or not isinstance(raw_assignments, dict):
        raise ValueError("roles.json must contain roles and assignments objects")
    roles: dict[str, EmployeeRole] = {}
    for key, item in raw_roles.items():
        if not isinstance(item, dict):
            raise ValueError(f"Invalid role definition: {key}")
        responsibilities = item.get("responsibilities")
        if not isinstance(responsibilities, list) or not responsibilities:
            raise ValueError(f"Role {key} must have responsibilities")
        roles[str(key)] = EmployeeRole(
            key=str(key),
            title=str(item.get("title") or "").strip(),
            objective=str(item.get("objective") or "").strip(),
            responsibilities=tuple(str(value).strip() for value in responsibilities),
        )
    assignments = {str(key): str(value) for key, value in raw_assignments.items()}
    unknown = sorted(set(assignments.values()) - set(roles))
    if unknown:
        raise ValueError(f"Unknown role assignments: {unknown}")
    return RoleRegistry(roles=roles, assignments=assignments)
