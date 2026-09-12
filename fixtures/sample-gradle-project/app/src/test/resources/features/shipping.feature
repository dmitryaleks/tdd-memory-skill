@logistics
Feature: Shipping

  Scenario: Parcels are quoted by weight
    Given a parcel weighing 3 kg
    When shipping is quoted
    Then the shipping cost is 6
