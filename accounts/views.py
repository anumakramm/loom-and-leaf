import os, json
import uuid
import stripe
from weasyprint import CSS, HTML
from products.models import *
from django.urls import reverse
from django.conf import settings
from django.contrib import messages
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from home.models import ShippingAddress
from django.contrib.auth.models import User
from django.template.loader import get_template
from accounts.models import Profile, Cart, CartItem, Order, OrderItem
from base.emails import send_account_activation_email
from django.views.decorators.http import require_POST
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect, HttpResponse
from django.contrib.auth import authenticate, login, logout
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import redirect, render, get_object_or_404
from accounts.forms import UserUpdateForm, UserProfileForm, ShippingAddressForm, CustomPasswordChangeForm


# Create your views here.


def login_page(request):
    if request.user.is_authenticated:
        return redirect('/')

    next_url = request.GET.get('next')  # Default to 'index' if 'next' is not provided
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user_obj = User.objects.filter(username=username)

        if not user_obj.exists():
            messages.warning(request, 'Account not found!')
            return HttpResponseRedirect(request.path_info)

        if not user_obj[0].profile.is_email_verified:
            messages.error(request, 'Account not verified!')
            return HttpResponseRedirect(request.path_info)

        # then authenticate user
        user_obj = authenticate(username=username, password=password)
        if user_obj:
            login(request, user_obj)
            messages.success(request, 'Login Successful.')
            
            # Check if the next URL is safe
            if url_has_allowed_host_and_scheme(url=next_url, allowed_hosts=request.get_host()):
                return redirect(next_url)
            else:
                return redirect('index')

        messages.warning(request, 'Invalid credentials.')
        return HttpResponseRedirect(request.path_info)

    return render(request, 'accounts/login.html')


def register_page(request):
    if request.user.is_authenticated:
        return redirect('/')
        
    if request.method == 'POST':
        username = request.POST.get('username')
        first_name = request.POST.get('first_name')
        last_name = request.POST.get('last_name')
        email = request.POST.get('email')
        password = request.POST.get('password')

        user_obj = User.objects.filter(username=username, email=email)

        if user_obj.exists():
            messages.info(request, 'Username or email already exists!')
            return HttpResponseRedirect(request.path_info)

        # if user not registered
        user_obj = User.objects.create(
            username=username, first_name=first_name, last_name=last_name, email=email)
        user_obj.set_password(password)

        
        user_obj.save()

        profile = Profile.objects.get(user=user_obj)
        profile.email_token = str(uuid.uuid4())
        profile.save()

        send_account_activation_email(email, profile.email_token)
        messages.success(request, "An email has been sent to your mail.")

        return HttpResponseRedirect(request.path_info)

    return render(request, 'accounts/register.html')


@login_required
def user_logout(request):
    logout(request)
    messages.warning(request, "Logged Out Successfully!")
    return redirect('index')

def activate_email_account(request, email_token):
    try:
        user = Profile.objects.get(email_token=email_token)
        user.is_email_verified = True
        user.save()
        messages.success(request, 'Account verification successful.')
        return redirect('login')
    except Exception as e:
        return HttpResponse('Invalid email token.')

@login_required
def add_to_cart(request, uid):
    try:
        variant = request.GET.get('size')
        if not variant:
            messages.error(request, 'Please select a size variant!')
            return redirect(request.META.get('HTTP_REFERER'))
        
        product = get_object_or_404(Product, uid=uid)

        cart, _ = Cart.objects.get_or_create(user=request.user, is_paid=False)
        size_variant = get_object_or_404(SizeVariant, size_name=variant)

        # Check if the cart item already exists in the cart
        cart_item, created = CartItem.objects.get_or_create(cart=cart, product=product, size_variant=size_variant)
        
        if not created:
            cart_item.quantity += 1
            cart_item.save()

        messages.success(request, 'Item added to cart successfully.')

    except Exception as e:
        print(e)
        messages.error(request, 'Error adding item to cart.')

    return redirect(reverse('cart'))


@login_required
def payment_success(request):
    # Get the Stripe session ID from the query parameters
    session_id = request.GET.get("session_id")
    if not session_id:
        messages.error(request, "Invalid session ID.")
        return redirect("cart")  # Redirect to the cart if no session_id is provided

    # Initialize Stripe with the secret key
    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        # Retrieve the checkout session details from Stripe
        session = stripe.checkout.Session.retrieve(session_id)

        # Check if the payment was successful
        if session.payment_status == "paid":
            # Get the cart based on metadata (saved during the Stripe session creation)
            cart_id = session.metadata.get("cart_id")
            cart = get_object_or_404(Cart, id=cart_id, user=request.user, is_paid=False)

            # Mark the cart as paid
            cart.is_paid = True
            cart.stripe_payment_intent_id = session.payment_intent  # Save payment intent for records
            cart.save()

            # Display a success message
            messages.success(request, "Payment successful! Thank you for your order.")

            # Redirect to a success page
            return render(request, "accounts/success.html", {"cart": cart})

        else:
            messages.error(request, "Payment was not successful. Please try again.")
            return redirect("cart")

    except stripe.error.StripeError as e:
        # Handle any Stripe-specific errors
        messages.error(request, f"Stripe error: {e.user_message}")
        return redirect("cart")

    except Exception as e:
        # Handle other errors
        messages.error(request, "An unexpected error occurred.")
        print(e)
        return redirect("cart")


@csrf_exempt
@login_required
def create_checkout_session(request):
    # Initialize Stripe with secret key
    stripe.api_key = settings.STRIPE_SECRET_KEY

    user = request.user
    try:
        # Get the user's cart
        cart = get_object_or_404(Cart, user=user, is_paid=False)

        # Calculate the total amount in cents (Stripe uses smallest currency unit)
        total_amount = int(cart.get_cart_total_price_after_coupon() * 100)

        if total_amount < 100:  # Minimum transaction amount in INR
            return JsonResponse({"error": "Cart total is too low for a transaction."}, status=400)

        # Create the Stripe Checkout Session
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                { 
                    "price_data": {
                        "currency": "cad",
                        "product_data": {
                            "name": "Sustainable Clothing Cart",
                            "description": f"Order from {user.username}",
                        },
                        "unit_amount": total_amount,
                    },
                    "quantity": 1,
                },
            ],
            mode="payment",
            success_url=f"{settings.SITE_URL}/accounts/success/?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{settings.SITE_URL}/accounts/cart/",
            metadata={
                "cart_id": cart.uid,
                "user_id": user.id,
            },
        )

        # Save the session ID to the cart for tracking
        cart.stripe_payment_intent_id = session.payment_intent
        cart.save()
        return JsonResponse({"id": session.id})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@login_required
def cart(request):
    cart_obj = None
    user = request.user

    # Initialize Stripe with secret key
    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        cart_obj = Cart.objects.get(is_paid=False, user=user)
    except Exception as e:
        print(e)
        messages.warning(request, "Your cart is empty. Please sign in or add a product to cart.")
        return HttpResponseRedirect(request.META.get('HTTP_REFERER'))

    if request.method == 'POST':
        coupon = request.POST.get('coupon')
        coupon_obj = Coupon.objects.filter(coupon_code__exact=coupon).first()

        if not coupon_obj:
            messages.warning(request, 'Invalid coupon code.')
            return HttpResponseRedirect(request.META.get('HTTP_REFERER'))

        if cart_obj and cart_obj.coupon:
            messages.warning(request, 'Coupon already exists.')
            return HttpResponseRedirect(request.META.get('HTTP_REFERER'))

        if coupon_obj and coupon_obj.is_expired:
            messages.warning(request, 'Coupon code expired.')
            return HttpResponseRedirect(request.META.get('HTTP_REFERER'))

        if cart_obj and coupon_obj and cart_obj.get_cart_total() < coupon_obj.minimum_amount:
            messages.warning(
                request, f'Amount should be greater than {coupon_obj.minimum_amount}')
            return HttpResponseRedirect(request.META.get('HTTP_REFERER'))

        if cart_obj and coupon_obj:
            cart_obj.coupon = coupon_obj
            cart_obj.save()
            messages.success(request, 'Coupon applied successfully.')
            return HttpResponseRedirect(request.META.get('HTTP_REFERER'))

    if cart_obj:
        cart_total = cart_obj.get_cart_total_price_after_coupon()

        if cart_total < 1.00:  # Assuming minimum amount is 1 INR
            messages.warning(
                request, 'Total amount in cart is less than the minimum required amount (1.00 INR). Please add a product to the cart.')
            return redirect('index')

        # Stripe payment intent
        try:
            intent = stripe.PaymentIntent.create(
                amount=int(cart_total * 100),  # Convert total to paise (smallest currency unit)
                currency='inr',
                metadata={'cart_id': cart_obj.uid},  # Include cart ID in metadata for tracking
            )
            cart_obj.stripe_payment_intent_id = intent['id']
            cart_obj.save()
        except stripe.error.StripeError as e:
            messages.error(request, f"Stripe error: {e.user_message}")
            return redirect('index')

    context = {
        'cart': cart_obj,
        'quantity_range': range(1, 6),
        'stripe_public_key': settings.STRIPE_PUBLIC_KEY,
    }
    return render(request, 'accounts/cart.html', context)

@require_POST
@login_required
def update_cart_item(request):
    try:
        data = json.loads(request.body)
        cart_item_id = data.get("cart_item_id")
        quantity = int(data.get("quantity"))

        cart_item = CartItem.objects.get(uid=cart_item_id, cart__user=request.user, cart__is_paid=False)
        cart_item.quantity = quantity
        cart_item.save()

        return JsonResponse({"success": True})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})


def remove_cart(request, uid):
    try:
        cart_item = get_object_or_404(CartItem, uid=uid)
        cart_item.delete()
        messages.success(request, 'Item removed from cart.')

    except Exception as e:
        print(e)
        messages.warning(request, 'Error removing item from cart.')

    return HttpResponseRedirect(request.META.get('HTTP_REFERER'))


def remove_coupon(request, cart_id):
    cart = Cart.objects.get(uid=cart_id)
    cart.coupon = None
    cart.save()

    messages.success(request, 'Coupon Removed.')
    return HttpResponseRedirect(request.META.get('HTTP_REFERER'))

@login_required
def payment_success(request):
    # Get the Stripe session ID from the query parameters
    session_id = request.GET.get("session_id")
    if not session_id:
        messages.error(request, "Invalid session ID.")
        return redirect("cart")  # Redirect to the cart if no session_id is provided

    # Initialize Stripe with the secret key
    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        # Retrieve the checkout session details from Stripe
        session = stripe.checkout.Session.retrieve(session_id)

        # Check if the payment was successful
        if session.payment_status == "paid":
            # Get the cart based on metadata (saved during the Stripe session creation)
            cart_id = session.metadata.get("cart_id")
            cart = get_object_or_404(Cart, uid=cart_id, user=request.user, is_paid=False)

            # Mark the cart as paid
            cart.is_paid = True
            cart.stripe_payment_intent_id = session.payment_intent  # Save payment intent for records
            cart.save()

            order = create_order(cart)
            # Display a success message
            messages.success(request, "Payment successful! Thank you for your order.")

            return render(request, 'payment_success/payment_success.html', { 'order': order })

        else:
            messages.error(request, "Payment was not successful. Please try again.")
            return redirect("cart")

    except stripe.error.StripeError as e:
        # Handle any Stripe-specific errors
        messages.error(request, f"Stripe error: {e.user_message}")
        return redirect("cart")

    except Exception as e:
        # Handle other errors
        messages.error(request, "An unexpected error occurred.")
        print(e)
        return redirect("cart")



# Payment success view
def success(request):
    order_id = request.GET.get('order_id')
    # cart = Cart.objects.get(razorpay_order_id = order_id)
    cart = get_object_or_404(Cart, razorpay_order_id = order_id)

    # Mark the cart as paid
    cart.is_paid = True
    cart.save()

    # Create the order after payment is confirmed
    order = create_order(cart)

    context = {'order_id': order_id, 'order': order}
    return render(request, 'payment_success/payment_success.html', context)


# HTML to PDF Conversion
def render_to_pdf(template_src, context_dict={}):
    template = get_template(template_src)
    html = template.render(context_dict)

    # Path to the staticfiles directory
    static_root = settings.MEDIA_ROOT


    print(
        os.path.join(static_root, 'css', 'bootstrap.css'),
    )
    # List all CSS files you need, now collected in STATIC_ROOT
    css_files = [
        os.path.join(static_root, 'css', 'bootstrap.css'),
        os.path.join(static_root, 'css', 'responsive.css'),
        os.path.join(static_root, 'css', 'ui.css'),
    ]

    # Create CSS objects for each file
    css_objects = [CSS(filename=css_file) for css_file in css_files]

    # Convert HTML to PDF with all CSS stylesheets applied
    pdf_file = HTML(string=html).write_pdf(stylesheets=css_objects)
    
    response = HttpResponse(pdf_file, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="invoice_{context_dict["order"].order_id}.pdf"'

    return response



def download_invoice(request, order_id):
    order = get_object_or_404(Order, order_id=order_id)
    order_items = order.order_items.all()

    context = {
        'order': order,
        'order_items': order_items,
    }

    pdf = render_to_pdf('accounts/order_pdf_generate.html', context)
    if pdf:
        return pdf
    return HttpResponse("Error generating PDF", status=400)



@login_required
def profile_view(request, username):
    user_name = get_object_or_404(User, username=username)
    user = request.user
    profile = user.profile

    user_form = UserUpdateForm(instance=user)
    profile_form = UserProfileForm(instance=profile)

    if request.method == 'POST':
        user_form = UserUpdateForm(request.POST, instance=user)
        profile_form = UserProfileForm(request.POST, request.FILES, instance=profile)
        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            messages.success(request, 'Your profile has been updated successfully!')
            return HttpResponseRedirect(request.META.get('HTTP_REFERER'))

    context = {
        'user_name' : user_name,
        'user_form': user_form,
        'profile_form': profile_form
    }

    return render(request, 'accounts/profile.html', context)


@login_required
def change_password(request):
    if request.method == 'POST':
        form = CustomPasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # Important!
            messages.success(request, 'Your password was successfully updated!')
            return HttpResponseRedirect(request.META.get('HTTP_REFERER'))
        else:
            messages.warning(request, 'Please correct the error below.')
    else:
        form = CustomPasswordChangeForm(request.user)
    return render(request, 'accounts/change_password.html', {'form': form})

@login_required
def update_shipping_address(request):
    shipping_address = ShippingAddress.objects.filter(
        user=request.user, current_address=True).first()

    if request.method == 'POST':
        form = ShippingAddressForm(request.POST, instance=shipping_address)
        if form.is_valid():
            shipping_address = form.save(commit=False)
            shipping_address.user = request.user
            shipping_address.current_address = True
            shipping_address.save()

            messages.success(request, "The Address Has Been Successfully Saved/Updated!")
            
            form = ShippingAddressForm()
        else:
            form = ShippingAddressForm(request.POST, instance=shipping_address)
    else:
        form = ShippingAddressForm(instance=shipping_address)

    return render(request, 'accounts/shipping_address_form.html', {'form': form})


# Order history view
@login_required
def order_history(request):
    orders = Order.objects.filter(user=request.user).order_by('-order_date')
    return render(request, 'accounts/order_history.html', {'orders': orders})


# Create an order view
def create_order(cart):
    order, created = Order.objects.get_or_create(
        user=cart.user,
        order_id=cart.stripe_payment_intent_id,
        payment_status="Paid",
        shipping_address=cart.user.profile.shipping_address,
        payment_mode="Credit Card",
        order_total_price=cart.get_cart_total(),
        coupon=cart.coupon,
        grand_total=cart.get_cart_total_price_after_coupon(),
    )

    # Create OrderItem instances for each item in the cart
    cart_items = CartItem.objects.filter(cart=cart)
    for cart_item in cart_items:
        OrderItem.objects.get_or_create(
            order=order,
            product=cart_item.product,
            size_variant=cart_item.size_variant,
            color_variant=cart_item.color_variant,
            quantity=cart_item.quantity,
            product_price=cart_item.get_product_price()
        )

    return order


# Order Details view
@login_required
def order_details(request, order_id):
    order = get_object_or_404(Order, order_id=order_id, user=request.user)
    order_items = OrderItem.objects.filter(order=order)
    context = {
        'order': order,
        'order_items': order_items,
        'order_total_price': sum(item.get_total_price() for item in order_items),
        'coupon_discount': order.coupon.discount_amount if order.coupon else 0,
        'grand_total': order.get_order_total_price()
    }
    return render(request, 'accounts/order_details.html', context)


# Delete user account feature
@login_required
def delete_account(request):
    if request.method == 'POST':
        user = request.user
        logout(request)
        user.delete()
        messages.success(request, "Your account has been deleted successfully.")
        return redirect('index')