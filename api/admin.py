from django.contrib import admin
from .models import Employee, LeaveRequest

admin.site.register(Employee)
admin.site.register(LeaveRequest)
