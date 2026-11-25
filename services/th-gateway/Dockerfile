FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY services/Directory.Build.props services/
COPY services/TenderHive.Contracts/TenderHive.Contracts.csproj services/TenderHive.Contracts/
COPY services/th-gateway/TenderHive.Gateway.csproj services/th-gateway/
RUN dotnet restore services/th-gateway/TenderHive.Gateway.csproj

COPY services/ services/
RUN dotnet publish services/th-gateway/TenderHive.Gateway.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "TenderHive.Gateway.dll"]
