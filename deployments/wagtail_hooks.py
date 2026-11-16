
from wagtail import hooks

from .views import deployment_target_viewset


@hooks.register("register_admin_viewset")
def register_deployment_target_viewset():
    return deployment_target_viewset
