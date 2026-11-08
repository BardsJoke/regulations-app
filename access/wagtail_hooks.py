from wagtail import hooks

from .views import RoleAssignmentViewSet


role_assignment_viewset = RoleAssignmentViewSet(
    "role_assignments"
)


@hooks.register("register_admin_viewset")
def register_role_assignment_viewset():
    return role_assignment_viewset