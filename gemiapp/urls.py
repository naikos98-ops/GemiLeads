from django.urls import path
from . import views
from . import billing

urlpatterns = [
    path("", views.home, name="home"),
    path("signup/", views.signup, name="signup"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("settings/", views.settings_view, name="settings"),
    path("radars/", views.radar_list, name="radar_list"),
    path("radars/new/", views.radar_create, name="radar_create"),
    path("radars/preview/", views.radar_preview, name="radar_preview"),
    path("radars/<int:pk>/", views.radar_detail, name="radar_detail"),
    path("radars/<int:pk>/edit/", views.radar_edit, name="radar_edit"),
    path("radars/<int:pk>/toggle/", views.radar_toggle, name="radar_toggle"),
    path("radars/<int:pk>/delete/", views.radar_delete, name="radar_delete"),
    path("radars/<int:pk>/export/", views.radar_export_csv, name="radar_export_csv"),
    path("leads/", views.lead_list, name="lead_list"),
    path("leads/<int:pk>/status/", views.lead_status, name="lead_status"),
    path("leads/<int:pk>/favorite/", views.lead_favorite, name="lead_favorite"),
    path("leads/<int:pk>/notes/", views.lead_notes, name="lead_notes"),
    path("companies/<str:gemi_number>/", views.company_detail, name="company_detail"),
    path("api/kads/", views.kad_search, name="kad_search"),
    path("export/", views.export_csv, name="export_csv"),
    path("verify/<uidb64>/<token>/", views.verify_email, name="verify_email"),
    path("unsubscribe/<token>/", views.unsubscribe, name="unsubscribe"),
    # Billing / Stripe
    path("pricing/", billing.pricing, name="pricing"),
    path("api/stripe/create-checkout-session/", billing.create_checkout_session, name="create_checkout_session"),
    path("api/stripe/customer-portal/", billing.customer_portal, name="customer_portal"),
    path("api/stripe/webhook/", billing.stripe_webhook, name="stripe_webhook"),
]
