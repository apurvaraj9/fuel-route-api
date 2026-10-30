from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from . import views

urlpatterns = [
    path("", views.planner_view, name="planner"),
    path("api/", views.api_index_view, name="api-index"),
    path("api/route/", views.RouteView.as_view(), name="route"),
    path("api/route/map/", views.legacy_map_view, name="route-map"),
    path("api/health/", views.health_view, name="health"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
