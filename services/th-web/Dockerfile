FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY services/Directory.Build.props services/
COPY services/TenderHive.Contracts/TenderHive.Contracts.csproj services/TenderHive.Contracts/
COPY services/th-web/TenderHive.Web.csproj services/th-web/
RUN dotnet restore services/th-web/TenderHive.Web.csproj

COPY services/ services/
RUN dotnet publish services/th-web/TenderHive.Web.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_URLS=http://+:8081
EXPOSE 8081
ENTRYPOINT ["dotnet", "TenderHive.Web.dll"]
