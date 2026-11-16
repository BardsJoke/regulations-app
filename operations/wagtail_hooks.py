from wagtail import hooks

from .views import StepTemplateViewSet


@hooks.register("register_admin_viewset")
def register_step_template_viewset():
    return StepTemplateViewSet("step_templates")