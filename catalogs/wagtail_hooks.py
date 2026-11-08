from wagtail import hooks

from .views import CatalogViewSetGroup


@hooks.register("register_admin_viewset")
def register_catalog_viewset_group():
    return CatalogViewSetGroup()