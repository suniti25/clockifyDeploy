from django.shortcuts import render
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from datetime import timedelta

from .serializers import LoginSerializer, RegisterSerializer


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)

        if serializer.is_valid():
            user = serializer.validated_data['user']
            refresh = RefreshToken.for_user(user)
            
            # Get user's role from profile
            role = user.profile.role if hasattr(user, 'profile') else 'EMPLOYEE'

            resp = Response({
                'access': str(refresh.access_token),
                'username': user.username,
                'role': role,
            }, status=status.HTTP_200_OK)

            # Set HttpOnly refresh token cookie for cross-origin HTTPS
            max_age = int(settings.SIMPLE_JWT.get('REFRESH_TOKEN_LIFETIME', timedelta(days=1)).total_seconds())

            resp.set_cookie(
                key="refresh_token",
                value=str(refresh),
                httponly=True,
                secure=True,  
                samesite="None",  
                path="/",  
                max_age=max_age,
            )


            return resp

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@method_decorator(csrf_exempt, name='dispatch')
class RefreshCookieView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        print("=" * 50)
        print("REFRESH TOKEN DEBUG")
        print("All cookies:", dict(request.COOKIES))
        print("Cookie header:", request.META.get('HTTP_COOKIE', 'No cookie header'))
        print("Origin:", request.META.get('HTTP_ORIGIN', 'No origin'))
        print("=" * 50)
        
        token = request.COOKIES.get('refresh_token')
        
        if not token:
            return Response({
                'detail': 'Refresh token missing', 
                'cookies_received': list(request.COOKIES.keys()),
                'cookie_header': request.META.get('HTTP_COOKIE', 'None'),
            }, status=status.HTTP_401_UNAUTHORIZED)

        try:
            refresh = RefreshToken(token)
            access = str(refresh.access_token)
            data = {'access': access}
            rotate = settings.SIMPLE_JWT.get('ROTATE_REFRESH_TOKENS', False)
            if rotate:
                if settings.SIMPLE_JWT.get('BLACKLIST_AFTER_ROTATION', False):
                    try:
                        refresh.blacklist()
                    except Exception:
                        pass

                # Mint a new refresh token and set cookie
                refresh.set_jti()
                refresh.set_exp()
                refresh.set_iat()
                refresh.outstand()

                resp = Response(data, status=status.HTTP_200_OK)
                max_age = int(settings.SIMPLE_JWT.get('REFRESH_TOKEN_LIFETIME', timedelta(days=1)).total_seconds())

                resp.set_cookie(
                    key='refresh_token',
                    value=str(refresh),
                    httponly=True,
                    secure=True, 
                    samesite="None",  
                    path='/', 
                    max_age=max_age,
                )
                return resp

            return Response(data, status=status.HTTP_200_OK)
        except TokenError as e:
            return Response({'detail': 'Invalid or expired refresh token', 'error': str(e)}, status=status.HTTP_401_UNAUTHORIZED)


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        resp = Response({'message': 'Logged out'}, status=status.HTTP_200_OK)
        resp.delete_cookie('refresh_token',
                            path='/', 
                            samesite='None',
                            secure=True)
        return resp


class RegisterView(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        
        if serializer.is_valid():
            user = serializer.save()
            
            return Response({
                'message': 'User registered successfully',
                'user': {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                }
            }, status=status.HTTP_201_CREATED)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
