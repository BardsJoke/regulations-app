from wagtail import hooks

from .views import change_ticket_viewset


@hooks.register("register_admin_viewset")
def register_change_ticket_viewset():
    return change_ticket_viewset