from django.urls import path

from . import views

urlpatterns = [
    path("", views.index_view, name="index"),
    path("api/route/", views.route_view, name="route"),
    path("api/route/map/", views.route_map_view, name="route-map"),
    path("api/health/", views.health_view, name="health"),
]
