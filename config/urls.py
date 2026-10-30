from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Fuel Route Planner admin"
admin.site.site_title = "Fuel Route Planner"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("routeplanner.urls")),
]
