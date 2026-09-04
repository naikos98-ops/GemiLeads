from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path

from gemiapp.seo import SITEMAPS, robots_txt
from gemiapp.views import RateLimitedLoginView, RateLimitedPasswordResetView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("robots.txt", robots_txt, name="robots_txt"),
    path("sitemap.xml", sitemap, {"sitemaps": SITEMAPS}, name="sitemap"),
    path("superadmin/", include("gemiapp.superadmin.urls")),
    path("login/", RateLimitedLoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password_reset/", RateLimitedPasswordResetView.as_view(html_email_template_name="registration/password_reset_email.html"), name="password_reset"),
    path("password_reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    # Social login only. Declared after the project's own login/logout/password_reset routes so
    # that those names keep resolving to the views above -- allauth ships its own views under
    # the same names, and the project's (rate-limited, custom-templated) ones must win.
    path("accounts/", include("allauth.urls")),
    path("", include("gemiapp.urls")),
]
