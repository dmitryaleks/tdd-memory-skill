# Fixture: the scenario on line 14 is asserted by tests/test_discovery.py.
@pricing
Feature: Orders

  Background:
    Given a clean basket

  Scenario: No discount for small orders
    Given the order total is 20
    When the order is priced
    Then the payable amount is 20

  @discount
  Scenario: Discount applied to large orders
    Given the order total is 150
    When the order is priced
    Then the payable amount is 135

  Scenario Outline: Tiered discount
    Given the order total is <total>
    When the order is priced
    Then the payable amount is <payable>

    Examples:
      | total | payable |
      | 100   | 95      |
      | 200   | 180     |
