from wagtail import hooks

from .dashboard import OperationsDashboardViewSet
from .views import StepTemplateViewSet


@hooks.register("register_admin_viewset")
def register_step_template_viewset():
    return StepTemplateViewSet("step_templates")


@hooks.register("register_admin_viewset")
def register_operations_dashboard_viewset():
    return OperationsDashboardViewSet()