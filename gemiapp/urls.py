from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("signup/", views.signup, name="signup"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("settings/", views.settings_view, name="settings"),
    path("api/kads/", views.kad_search, name="kad_search"),
    path("export/", views.export_csv, name="export_csv"),
]
