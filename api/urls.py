from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import EmployeeViewSet, LeaveRequestViewSet, login_view, leave_list, my_leaves, approve_leave, reject_leave

router = DefaultRouter()
router.register('employees', EmployeeViewSet)
router.register('leaves', LeaveRequestViewSet, basename='leaves')

urlpatterns = [
    path('', include(router.urls)),
    path('login/', login_view),
    path('all-leaves/', leave_list),
    path('my-leaves/', my_leaves),
    path('leaves/<int:pk>/approve/', approve_leave),
    path('leaves/<int:pk>/reject/', reject_leave),
]