package com.acme.steps;

import static org.junit.jupiter.api.Assertions.assertEquals;

import io.cucumber.java.en.Given;
import io.cucumber.java.en.Then;
import io.cucumber.java.en.When;

/** Glue that never touches the target; its scenarios must not be discovered. */
public class ShippingSteps {

    private int weight;
    private int cost;

    @Given("a parcel weighing {int} kg")
    public void aParcelWeighing(int kg) {
        weight = kg;
    }

    @When("shipping is quoted")
    public void shippingIsQuoted() {
        cost = weight * 2;
    }

    @Then("the shipping cost is {int}")
    public void theShippingCostIs(int expected) {
        assertEquals(expected, cost);
    }
}
