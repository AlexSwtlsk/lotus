from django.forms.models import model_to_dict
import dateutil.parser as parser
from ..models import Customer, Event, Subscription, BillingPlan, PlanComponent
from ..serializers import EventSerializer, SubscriptionSerializer, CustomerSerializer
from rest_framework.views import APIView
from django_q.tasks import async_task
from ..tasks import generate_invoice
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse, HttpRequest
import json

from rest_framework import viewsets
from ..permissions import HasUserAPIKey
from rest_framework.response import Response

# Create your views here.


class EventViewSet(viewsets.ModelViewSet):
    queryset = Event.objects.all()
    serializer_class = EventSerializer


class SubscriptionViewSet(viewsets.ModelViewSet):
    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer


class SubscriptionView(APIView):
    permission_classes = [HasUserAPIKey]

    def get(self, request, format=None):
        """
        List active subscriptions. If customer_id is provided, only return subscriptions for that customer.
        """
        if "customer_id" in request.query_params:
            customer_id = request.query_params["customer_id"]
            try:
                customer = Customer.objects.get(customer_id=customer_id)
            except Customer.DoesNotExist:
                return HttpResponseBadRequest("Customer does not exist")
            subscriptions = Subscription.objects.filter(
                customer=customer, status="active"
            )
            serializer = SubscriptionSerializer(subscriptions, many=True)
            return Response(serializer.data)
        else:
            subscriptions = Subscription.objects.filter(status="active")
            serializer = SubscriptionSerializer(subscriptions, many=True)
            return Response(serializer.data)

    def post(self, request, format=None):
        """
        Create a new subscription, joining a customer and a plan.
        """
        data = request.data
        customer_qs = Customer.objects.filter(customer_id=data["customer_id"])
        start_date = parser.parse(data["start_date"])

        if len(customer_qs) < 1:
            return Response(
                {
                    "error": "Customer with customer_id {} does not exist".format(
                        data["customer_id"]
                    )
                },
                status=400,
            )
        else:
            customer = customer_qs[0]
        plan_qs = BillingPlan.objects.filter(plan_id=data["plan_id"])
        if len(plan_qs) < 1:
            return Response(
                {
                    "error": "Plan with plan_id {} does not exist".format(
                        data["plan_id"]
                    )
                },
                status=400,
            )
        else:
            plan = plan_qs[0]
            end_date = plan.subscription_end_date(start_date)

        subscription = Subscription.objects.create(
            customer=customer,
            start_date=start_date,
            end_date=end_date,
            billing_plan=plan,
            status="active",
        )
        subscription.save()

        serializer_context = {
            "request": request,
        }

        return Response("Subscription Created", status=201)


class CustomerView(APIView):

    permission_classes = [HasUserAPIKey]

    def get(self, request, format=None):
        """
        Return a list of all customers.
        """
        customers = Customer.objects.all()
        serializer = CustomerSerializer(customers, many=True)
        return Response(serializer.data)

    def post(self, request, format=None):
        """
        Create a new customer.
        """

        serializer = CustomerSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)


class UsageView(APIView):

    permission_classes = [HasUserAPIKey]

    def get(self, request, format=None):
        """
        Return current usage for a customer during a given billing period.
        """
        customer_id = request.query_params["customer_id"]
        customer = Customer.objects.get(customer_id=customer_id)

        customer_subscriptions = Subscription.objects.filter(
            customer=customer, status="active"
        )

        usage_summary = {}
        for subscription in customer_subscriptions:

            plan = subscription.billing_plan
            flat_rate = int(plan.flat_rate.amount)
            plan_start_timestamp = subscription.start_date
            plan_end_timestamp = subscription.end_date

            plan_components_qs = PlanComponent.objects.filter(billing_plan=plan.id)
            subtotal_cost = 0
            plan_components_summary = {}
            # For each component of the plan, calculate usage/cost
            for plan_component in plan_components_qs:
                billable_metric = plan_component.billable_metric
                event_name = billable_metric.event_name
                aggregation_type = billable_metric.aggregation_type
                subtotal_usage = 0.0

                events = Event.objects.filter(
                    event_name=event_name,
                    time_created__gte=plan_start_timestamp,
                    time_created__lte=plan_end_timestamp,
                )

                if aggregation_type == "count":
                    subtotal_usage = len(events) - plan_component.free_metric_quanity
                elif aggregation_type == "sum":
                    property_name = billable_metric.property_name
                    for event in events:
                        properties_dict = event.properties
                        subtotal_usage += float(properties_dict[property_name])
                    subtotal_usage -= plan_component.free_metric_quantity

                elif aggregation_type == "max":
                    property_name = billable_metric.property_name
                    for event in events:
                        properties_dict = event.properties
                        subtotal_usage = max(
                            subtotal_usage, float(properties_dict[property_name])
                        )
                subtotal_cost += int(
                    (subtotal_usage * plan_component.cost_per_metric).amount
                )

                plan_components_summary[str(plan_component)] = {
                    "subtotal_cost": "$" + subtotal_cost,
                    "subtotal_usage": subtotal_usage,
                }

            usage_summary[plan.name] = {
                "total_usage_cost": subtotal_cost,
                "flat_rate_cost": flat_rate,
                "components": plan_components_summary,
                "current_amount_due": subtotal_cost + flat_rate,
                "billing_start_date": plan_start_timestamp,
                "billing_end_date": plan_end_timestamp,
            }

        usage_summary["# of Active Subscriptions"] = len(usage_summary)
        return Response(usage_summary)