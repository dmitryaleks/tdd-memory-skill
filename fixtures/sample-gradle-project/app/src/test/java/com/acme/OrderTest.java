package com.acme;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class OrderTest {

    @Test
    void totalsLines() {
        Order order = new Order();
        order.addLine(30);
        order.addLine(12);
        assertEquals(42, order.total());
    }

    @Test
    void appliesDiscountToLargeOrders() {
        Order order = new Order();
        order.addLine(150);
        assertEquals(135, order.payable());
    }

    @Test
    void leavesSmallOrdersAlone() {
        Order order = new Order();
        order.addLine(20);
        assertEquals(20, order.payable());
    }
}
