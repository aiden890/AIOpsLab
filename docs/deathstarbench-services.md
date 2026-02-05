# DeathStarBench Applications - Microservices Documentation

This document describes all microservices used in AIOpsLab's DeathStarBench applications.

---

## Microservice vs Backing Service

In microservice architecture, there's an important distinction between different types of services:

| Type | Examples | Description |
|------|----------|-------------|
| **Microservice** | frontend, recommendation, geo, user-service | Contains **business logic**, exposes APIs (gRPC/REST), written in Go/Python/etc. |
| **Backing Service** | MongoDB, Redis, Memcached, Consul | **Infrastructure** that stores data or provides platform capabilities. No business logic. |

### True Microservices (Business Logic)

These services contain application code and implement business functionality:

```
Hotel Reservation: frontend, geo, profile, rate, recommendation, reservation, search, user
Social Network:    compose-post-service, user-service, text-service, media-service, etc.
```

### Backing/Infrastructure Services

These are third-party infrastructure components that microservices depend on:

```
Databases:    mongodb-*, post-storage-mongodb, user-mongodb
Caches:       memcached-*, redis-*, user-memcached
Tracing:      jaeger
Discovery:    consul
```

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    MICROSERVICES                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ frontend │→ │  search  │→ │ geo, rate, profile,  │  │
│  │ (gateway)│  │          │  │ recommendation, user │  │
│  └──────────┘  └──────────┘  └──────────────────────┘  │
│        │              │                  │              │
│        ▼              ▼                  ▼              │
│  ┌─────────────────────────────────────────────────┐   │
│  │              BACKING SERVICES                    │   │
│  │  MongoDB (data)  │  Memcached (cache)  │ Consul  │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

> **Note:** In Kubernetes, all components run as "Services" (K8s resource), so they appear similar when running `kubectl get svc`. However, architecturally, only the core services with business logic are true "microservices."

---

## Social Network

**Namespace:** `test-social-network`

**Description:** A social network with unidirectional follow relationships, implemented with loosely-coupled microservices, communicating with each other via Thrift RPCs.

**Helm Config:**
- Release Name: `social-network`
- Chart Path: `socialNetwork/helm-chart/socialnetwork`

### Supported Operations

- Create text post (optional media: image, video, shortened URL, user tag)
- Read post
- Read entire user timeline
- Receive recommendations on which users to follow
- Search database for user or post
- Register/Login using user credentials
- Follow/Unfollow user

### Core Services

| Service | Description | Type |
|---------|-------------|------|
| nginx-thrift | API gateway and load balancer for incoming requests | gateway |
| compose-post-service | Composes new posts by coordinating with other services | business-logic |
| home-timeline-service | Manages and serves home timeline feeds | business-logic |
| user-timeline-service | Manages and serves individual user timelines | business-logic |
| post-storage-service | Stores and retrieves posts | business-logic |
| social-graph-service | Manages follow/unfollow relationships between users | business-logic |
| user-service | Handles user registration, login, and profile management | business-logic |
| user-mention-service | Processes @mentions in posts | business-logic |
| text-service | Processes post text content | business-logic |
| unique-id-service | Generates unique IDs for posts and other entities | business-logic |
| url-shorten-service | Shortens URLs included in posts | business-logic |
| media-service | Handles media uploads and storage | business-logic |
| media-frontend | Frontend for media content serving | frontend |

### Storage Services

#### MongoDB

| Service | Description |
|---------|-------------|
| post-storage-mongodb | Stores post data |
| social-graph-mongodb | Stores social graph relationships |
| user-mongodb | Stores user profiles and credentials |
| user-timeline-mongodb | Stores user timeline data |
| url-shorten-mongodb | Stores URL mappings |
| media-mongodb | Stores media metadata |

#### Redis

| Service | Description |
|---------|-------------|
| home-timeline-redis | Caches home timeline data |
| social-graph-redis | Caches social graph data |
| user-timeline-redis | Caches user timeline data |

#### Memcached

| Service | Description |
|---------|-------------|
| post-storage-memcached | Caches post data |
| user-memcached | Caches user data |
| url-shorten-memcached | Caches URL mappings |
| media-memcached | Caches media metadata |

### Infrastructure Services

| Service | Description | Type |
|---------|-------------|------|
| jaeger | Distributed tracing for request monitoring | observability |

---

## Hotel Reservation

**Namespace:** `test-hotel-reservation`

**Description:** A hotel reservation application built with Go and gRPC, providing backend in-memory and persistent databases, a recommender system for hotel recommendations, and functionality to place reservations.

**Helm Config:**
- Release Name: `hotel-reservation`
- Chart Path: `hotelReservation/helm-chart/hotelreservation`
- Kubernetes Deploy Path: `hotelReservation/kubernetes`

### Supported Operations

- Get profile and rates of nearby hotels available during given time periods
- Recommend hotels based on user provided metrics
- Place reservations

### Core Services

| Service | Description | Type |
|---------|-------------|------|
| frontend | API gateway handling incoming HTTP requests | gateway |
| search | Searches for available hotels based on criteria | business-logic |
| geo | Handles geolocation queries for nearby hotels | business-logic |
| profile | Manages hotel profile information | business-logic |
| rate | Manages hotel room rates and pricing | business-logic |
| recommendation | Provides hotel recommendations based on user preferences | business-logic |
| reservation | Handles room reservations and booking | business-logic |
| user | Manages user authentication and profiles | business-logic |

### Storage Services

#### MongoDB

| Service | Description |
|---------|-------------|
| mongodb-geo | Stores geolocation data |
| mongodb-profile | Stores hotel profiles |
| mongodb-rate | Stores rate information |
| mongodb-recommendation | Stores recommendation data |
| mongodb-reservation | Stores reservation records |
| mongodb-user | Stores user data |

#### Memcached

| Service | Description |
|---------|-------------|
| memcached-profile | Caches hotel profile data |
| memcached-rate | Caches rate data |
| memcached-reserve | Caches reservation data |

### Infrastructure Services

| Service | Description | Type |
|---------|-------------|------|
| consul | Service discovery and configuration | service-mesh |
| jaeger | Distributed tracing for request monitoring | observability |

---

## Summary

| Application | Total Services | Core | MongoDB | Redis | Memcached | Infrastructure |
|-------------|----------------|------|---------|-------|-----------|----------------|
| Social Network | 31 | 13 | 6 | 3 | 4 | 1 |
| Hotel Reservation | 19 | 8 | 6 | 0 | 3 | 2 |

---

## Complete Service Lists

### Social Network - All Services (31)

```bash
# Command to list all services
ls aiopslab-applications/socialNetwork/helm-chart/socialnetwork/charts/
```

```
compose-post-service
home-timeline-redis
home-timeline-service
jaeger
media-frontend
media-memcached
media-mongodb
media-service
nginx-thrift
post-storage-memcached
post-storage-mongodb
post-storage-service
social-graph-mongodb
social-graph-redis
social-graph-service
text-service
unique-id-service
url-shorten-memcached
url-shorten-mongodb
url-shorten-service
user-memcached
user-mention-service
user-mongodb
user-service
user-timeline-mongodb
user-timeline-redis
user-timeline-service
```

### Hotel Reservation - All Services (19)

```bash
# Command to list all services
ls aiopslab-applications/hotelReservation/helm-chart/hotelreservation/charts/
```

```
consul
frontend
geo
jaeger
memcached-profile
memcached-rate
memcached-reserve
mongodb-geo
mongodb-profile
mongodb-rate
mongodb-recommendation
mongodb-reservation
mongodb-user
profile
rate
recommendation
reservation
search
user
```

---

## Useful Commands

### List Microservices from Helm Charts

```bash
# List all Social Network microservices
ls aiopslab-applications/socialNetwork/helm-chart/socialnetwork/charts/

# List all Hotel Reservation microservices
ls aiopslab-applications/hotelReservation/helm-chart/hotelreservation/charts/
```

### View Running Services in Kubernetes

```bash
# List Social Network pods
kubectl get pods -n test-social-network

# List Hotel Reservation pods
kubectl get pods -n test-hotel-reservation

# List all services in Social Network namespace
kubectl get svc -n test-social-network

# List all services in Hotel Reservation namespace
kubectl get svc -n test-hotel-reservation
```

### View Helm Releases

```bash
# List Helm releases
helm list -A

# Get Social Network release details
helm status social-network -n test-social-network

# Get Hotel Reservation release details
helm status hotel-reservation -n test-hotel-reservation
```

### Debug Services

```bash
# Describe a specific pod (e.g., frontend in hotel-reservation)
kubectl describe pod -l app=frontend -n test-hotel-reservation

# View logs for a specific service
kubectl logs -l app=frontend -n test-hotel-reservation --tail=100

# Check service endpoints
kubectl get endpoints -n test-social-network
```
