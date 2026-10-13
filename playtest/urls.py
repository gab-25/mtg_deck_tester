"""Match routes."""

from django.urls import path

from . import views

urlpatterns = [
    path("matches", views.match_list, name="match_list"),
    path("matches/new", views.new_match, name="new_match"),
    path("matches/create", views.create_match, name="create_match"),
    path("matches/<uuid:match_id>", views.match_detail, name="match_detail"),
    path("matches/<uuid:match_id>/live", views.match_live, name="match_live"),
    path("matches/<uuid:match_id>/stop", views.stop_match, name="stop_match"),
    path("matches/<uuid:match_id>/delete", views.delete_match, name="delete_match"),
]
