from wagtail.users.forms import (
    UserCreationForm,
    UserEditForm,
)


class CustomUserCreationForm(UserCreationForm):

    class Meta(UserCreationForm.Meta):
        fields = UserCreationForm.Meta.fields | {"role"}


class CustomUserEditForm(UserEditForm):

    class Meta(UserEditForm.Meta):
        fields = UserEditForm.Meta.fields | {"role"}