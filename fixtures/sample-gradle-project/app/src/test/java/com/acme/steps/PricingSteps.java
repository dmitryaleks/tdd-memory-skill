package com.acme.steps;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.acme.Order;
import io.cucumber.java.en.Given;
import io.cucumber.java.en.Then;
import io.cucumber.java.en.When;

/** Glue that drives the target directly - hop 1 of scenario discovery. */
public class PricingSteps {

    private Order order;
    private int payable;

    @Given("a clean basket")
    public void aCleanBasket() {
        order = new Order();
    }

    @Given("the order total is {int}")
    public void theOrderTotalIs(int amount) {
        order.addLine(amount);
    }

    @When("the order is priced")
    public void theOrderIsPriced() {
        payable = order.payable();
    }

    @Then("the payable amount is {int}")
    public void thePayableAmountIs(int expected) {
        assertEquals(expected, payable);
    }
}
