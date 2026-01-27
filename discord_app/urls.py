from django.urls import path
from .views import discord_interactions

urlpatterns = [
    path('interactions', discord_interactions, name='discord_interactions'),
]
