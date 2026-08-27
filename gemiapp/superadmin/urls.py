from django.urls import path
from . import views

app_name = "superadmin"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("users/", views.user_list, name="user_list"),
    path("users/<int:user_id>/", views.user_detail, name="user_detail"),
    path("users/<int:user_id>/active/", views.user_toggle_active, name="user_toggle_active"),
    path("users/<int:user_id>/complimentary/", views.user_complimentary, name="user_complimentary"),
    path("users/<int:user_id>/send-yesterday-digest/", views.user_send_yesterday_digest, name="user_send_yesterday_digest"),
    path("subscriptions/", views.subscription_list, name="subscription_list"),
    path("client-finder/", views.client_finder, name="client_finder"),
    path("client-finder/send/", views.client_finder_send, name="client_finder_send"),
    path("radars/", views.radar_list, name="radar_list"),
    path("radars/<int:radar_id>/", views.radar_detail, name="radar_detail"),
    path("leads/", views.lead_list, name="lead_list"),
    path("leads/<int:lead_id>/", views.lead_detail, name="lead_detail"),
    path("pipeline/", views.pipeline_overview, name="pipeline_overview"),
    path("pipeline/run/", views.pipeline_run_now, name="pipeline_run_now"),
    path("digests/", views.digest_list, name="digest_list"),
    path("digests/<int:delivery_id>/retry/", views.digest_retry, name="digest_retry"),
    path("health/", views.health_overview, name="health_overview"),
    path("audit/", views.audit_list, name="audit_list"),
    path("accounts/", views.account_list, name="account_list"),
    path("accounts/create/", views.account_create, name="account_create"),
    path("backup/", views.backup_download, name="backup_download"),
    path("impersonate/<int:user_id>/", views.impersonate_start, name="impersonate_start"),
    path("impersonate/stop/", views.impersonate_stop, name="impersonate_stop"),
]
